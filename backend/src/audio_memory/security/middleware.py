from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from email import policy
from email.message import EmailMessage, Message
from email.utils import collapse_rfc2231_value
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser

from audio_memory.security.local_session import (
    IdempotencyClaim,
    LocalSessionSecurity,
    SessionCapacityError,
    StoredResponse,
)


ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]],
    Awaitable[None],
]
MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_PENDING_WAIT_SECONDS = 120
logger = logging.getLogger("uvicorn.error")


class LocalWebSecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        security: LocalSessionSecurity,
        allowed_port: int,
    ) -> None:
        self.app = app
        self.security = security
        self.allowed_port = allowed_port
        self.allowed_hosts = {
            f"127.0.0.1:{allowed_port}",
            f"localhost:{allowed_port}",
            f"[::1]:{allowed_port}",
        }

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path != "/api" and not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        trusted_host = self._trusted_host(headers)
        if trusted_host is None:
            await _send_error(send, 403, "untrusted_host", "Untrusted local Host")
            return

        method = scope["method"].upper()
        if path == "/api/session" and method == "GET":
            if not self._trusted_optional_origin(headers, trusted_host):
                await _send_error(send, 403, "untrusted_origin", "Untrusted Origin")
                return
            try:
                token = await asyncio.to_thread(self.security.issue_session)
            except SessionCapacityError:
                await _send_error(
                    send,
                    429,
                    "session_capacity",
                    "Local session capacity reached; retry after an existing session expires",
                )
                return
            body = json.dumps({"token": token}, separators=(",", ":")).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        if method not in MUTATION_METHODS:
            await self.app(scope, receive, send)
            return
        if not self._trusted_required_origin(headers, trusted_host):
            await _send_error(send, 403, "untrusted_origin", "Trusted Origin required")
            return

        token = _single_header(headers, b"x-audio-memory-session")
        try:
            decoded_token = token.decode("ascii") if token is not None else None
        except UnicodeDecodeError:
            decoded_token = None
        session_hash = await asyncio.to_thread(
            self.security.authenticate,
            decoded_token,
        )
        if session_hash is None:
            await _send_error(send, 401, "invalid_session", "Valid local session required")
            return
        key_value = _single_header(headers, b"idempotency-key")
        if key_value is None or not _valid_idempotency_key(key_value):
            await _send_error(send, 400, "invalid_idempotency_key", "Idempotency-Key required")
            return
        idempotency_key = key_value.decode("ascii")

        body_file, body_hash = await _capture_body(receive, headers)
        body_hash = _request_hash(scope, headers, body_hash)
        endpoint = _endpoint(scope)
        claim = await asyncio.to_thread(
            self.security.claim,
            session_hash=session_hash,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
        )
        if claim.state == "pending":
            claim = await self._wait_for_completion(
                session_hash, endpoint, idempotency_key, body_hash
            )
        if claim.state == "replay":
            assert claim.response is not None
            body_file.close()
            await _send_stored(send, claim.response)
            return
        if claim.state == "mismatch":
            body_file.close()
            await _send_error(
                send,
                409,
                "idempotency_key_reused",
                "Idempotency-Key was already used with a different request body",
            )
            return
        if claim.state in {"pending", "capacity"}:
            body_file.close()
            code = (
                "request_in_progress"
                if claim.state == "pending"
                else "idempotency_capacity"
            )
            await _send_error(send, 409, code, "Mutation cannot be claimed safely")
            return

        response_messages: list[dict] = []

        async def capture_send(message: dict) -> None:
            response_messages.append(message)

        try:
            await self.app(scope, _replay_receive(body_file), capture_send)
        except Exception:
            logger.exception("Protected mutation failed before response publication")
            stored = _error_response(
                500, "internal_error", "Local service failed to complete the action"
            )
        else:
            stored = _stored_response(response_messages)
        finally:
            body_file.close()
        await asyncio.to_thread(
            self.security.complete,
            session_hash=session_hash,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
            response=stored,
        )
        await _send_stored(send, stored)

    def _trusted_host(self, headers: list[tuple[bytes, bytes]]) -> str | None:
        host = _single_header(headers, b"host")
        if host is None:
            return None
        try:
            decoded = host.decode("ascii").lower()
        except UnicodeDecodeError:
            return None
        return decoded if decoded in self.allowed_hosts else None

    def _trusted_optional_origin(
        self, headers: list[tuple[bytes, bytes]], trusted_host: str
    ) -> bool:
        if any(
            name == b"sec-fetch-site" and value.strip().lower() == b"cross-site"
            for name, value in headers
        ):
            return False
        origins = [value for name, value in headers if name == b"origin"]
        if not origins:
            return True
        return len(origins) == 1 and self._origin_allowed(origins[0], trusted_host)

    def _trusted_required_origin(
        self, headers: list[tuple[bytes, bytes]], trusted_host: str
    ) -> bool:
        origins = [value for name, value in headers if name == b"origin"]
        return len(origins) == 1 and self._origin_allowed(origins[0], trusted_host)

    def _origin_allowed(self, raw_origin: bytes, trusted_host: str) -> bool:
        try:
            origin = raw_origin.decode("ascii")
            parsed = urlsplit(origin)
        except (UnicodeDecodeError, ValueError):
            return False
        return (
            origin.lower() == f"http://{trusted_host}"
            and parsed.scheme == "http"
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and parsed.query == ""
            and parsed.fragment == ""
        )

    async def _wait_for_completion(
        self, session_hash: str, endpoint: str, key: str, body_hash: str
    ) -> IdempotencyClaim:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MAX_PENDING_WAIT_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(0.02)
            claim = await asyncio.to_thread(
                self.security.claim,
                session_hash=session_hash,
                endpoint=endpoint,
                idempotency_key=key,
                body_hash=body_hash,
            )
            if claim.state != "pending":
                return claim
        return IdempotencyClaim("pending")


def _headers(scope: dict) -> list[tuple[bytes, bytes]]:
    return [(name.lower(), value) for name, value in scope.get("headers", [])]


def _single_header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    values = [value for header_name, value in headers if header_name == name]
    return values[0] if len(values) == 1 else None


def _valid_idempotency_key(value: bytes) -> bool:
    if not 1 <= len(value) <= 200:
        return False
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return False
    return decoded.strip() == decoded and all(
        0x21 <= ord(char) <= 0x7E for char in decoded
    )


def _endpoint(scope: dict) -> str:
    return f"{scope['method'].upper()} {scope.get('path', '')}"


def _request_hash(
    scope: dict, headers: list[tuple[bytes, bytes]], body_hash: str
) -> str:
    digest = hashlib.sha256()
    digest.update(b"query\0")
    digest.update(scope.get("query_string", b""))
    for name, value in headers:
        if name == b"content-type":
            normalized_value = _normalized_content_type(value)
        elif name == b"x-configuration-session":
            normalized_value = value
        else:
            continue
        digest.update(b"\0header\0")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(normalized_value).to_bytes(4, "big"))
        digest.update(normalized_value)
    digest.update(b"\0body\0")
    digest.update(body_hash.encode("ascii"))
    return digest.hexdigest()


def _normalized_content_type(value: bytes) -> bytes:
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        parts = [b"raw", value]
    else:
        if _content_type_has_parse_defects(decoded):
            parts = [b"raw", decoded.encode("utf-8")]
        else:
            parts = _normalized_parsed_content_type(decoded, value)

    encoded = bytearray(len(parts).to_bytes(4, "big"))
    for part in parts:
        encoded.extend(len(part).to_bytes(4, "big"))
        encoded.extend(part)
    return bytes(encoded)


def _normalized_parsed_content_type(decoded: str, raw_value: bytes) -> list[bytes]:
    message = Message()
    message["content-type"] = decoded
    media_type = message.get_content_type().lower()
    raw_media_type = decoded.split(";", 1)[0].strip().lower()
    parsed_parameters = message.get_params(header="content-type") or []
    if not _valid_media_type(raw_media_type) or media_type != raw_media_type:
        return [b"raw", raw_value]

    parameters: list[tuple[str, str]] = []
    for name, parameter_value in parsed_parameters[1:]:
        normalized_name = name.lower()
        if not _valid_mime_token(normalized_name):
            return [b"raw", raw_value]
        if media_type.startswith("multipart/") and normalized_name == "boundary":
            continue
        try:
            normalized_value = collapse_rfc2231_value(parameter_value, errors="strict")
        except (LookupError, UnicodeDecodeError):
            return [b"raw", raw_value]
        parameters.append((normalized_name, normalized_value))

    parts = [b"parsed", media_type.encode("ascii")]
    for name, parameter_value in sorted(parameters):
        parts.extend((name.encode("ascii"), parameter_value.encode("utf-8")))
    return parts


def _content_type_has_parse_defects(value: str) -> bool:
    message = EmailMessage(policy=policy.default)
    try:
        message["content-type"] = value
    except (TypeError, ValueError):
        return True
    return bool(message["content-type"].defects)


def _valid_media_type(value: str) -> bool:
    major, separator, minor = value.partition("/")
    return (
        separator == "/"
        and "/" not in minor
        and _valid_mime_token(major)
        and _valid_mime_token(minor)
    )


def _valid_mime_token(value: str) -> bool:
    extra = "!#$%&'*+-.^_`|~"
    return bool(value) and all(
        character.isalnum() or character in extra for character in value
    )


async def _capture_body(receive, headers: list[tuple[bytes, bytes]]):
    captured = SpooledTemporaryFile(max_size=1024 * 1024)
    digest = hashlib.sha256()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            captured.close()
            raise ConnectionError("client disconnected before request body completed")
        body = message.get("body", b"")
        captured.write(body)
        digest.update(body)
        if not message.get("more_body", False):
            break
    captured.seek(0)
    content_type = _single_header(headers, b"content-type") or b""
    if content_type.lower().split(b";", 1)[0].strip() == b"application/json":
        raw = captured.read()
        captured.seek(0)
        try:
            canonical = json.dumps(
                json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            return captured, hashlib.sha256(canonical).hexdigest()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if content_type.lower().split(b";", 1)[0].strip() == b"multipart/form-data":
        message = Message()
        try:
            message["content-type"] = content_type.decode("ascii")
            boundary = message.get_param("boundary", header="content-type")
            if isinstance(boundary, str) and boundary:
                normalized = _multipart_body_hash(captured, boundary.encode("ascii"))
                captured.seek(0)
                return captured, normalized
        except (UnicodeDecodeError, UnicodeEncodeError, MultipartParseError, ValueError):
            pass
    return captured, digest.hexdigest()


def _multipart_body_hash(body_file, boundary: bytes) -> str:
    digest = hashlib.sha256()
    headers: list[tuple[bytes, bytes]] = []
    header_field = bytearray()
    header_value = bytearray()
    part_digest = None
    part_size = 0
    ended = False

    def add_blob(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def on_part_begin() -> None:
        nonlocal headers, part_digest, part_size
        headers = []
        part_digest = hashlib.sha256()
        part_size = 0

    def on_header_begin() -> None:
        header_field.clear()
        header_value.clear()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header_field.extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header_value.extend(data[start:end])

    def on_header_end() -> None:
        headers.append((bytes(header_field).lower(), bytes(header_value)))

    def on_part_data(data: bytes, start: int, end: int) -> None:
        nonlocal part_size
        assert part_digest is not None
        chunk = data[start:end]
        part_digest.update(chunk)
        part_size += len(chunk)

    def on_part_end() -> None:
        assert part_digest is not None
        digest.update(b"part\0")
        digest.update(len(headers).to_bytes(4, "big"))
        for field, value in headers:
            add_blob(field)
            add_blob(value)
        digest.update(part_size.to_bytes(8, "big"))
        digest.update(part_digest.digest())

    def on_end() -> None:
        nonlocal ended
        ended = True

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_begin": on_header_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        },
    )
    body_file.seek(0)
    while chunk := body_file.read(64 * 1024):
        parser.write(chunk)
    parser.finalize()
    if not ended:
        raise MultipartParseError("multipart body did not contain a closing boundary")
    return digest.hexdigest()


def _replay_receive(body_file):
    finished = False
    body_file.seek(0, 2)
    remaining = body_file.tell()
    body_file.seek(0)

    async def receive() -> dict:
        nonlocal finished, remaining
        if finished:
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = body_file.read(min(64 * 1024, remaining))
        remaining -= len(chunk)
        more = remaining > 0
        if not more:
            finished = True
        return {"type": "http.request", "body": chunk, "more_body": more}

    return receive


def _stored_response(messages: list[dict]) -> StoredResponse:
    starts = [message for message in messages if message["type"] == "http.response.start"]
    if len(starts) != 1:
        raise RuntimeError("mutation returned an invalid ASGI response")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return StoredResponse(
        status=starts[0]["status"],
        headers=tuple(starts[0].get("headers", [])),
        body=body,
    )


async def _send_stored(send, response: StoredResponse) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": response.status,
            "headers": list(response.headers),
        }
    )
    await send({"type": "http.response.body", "body": response.body})


async def _send_error(send, status: int, code: str, message: str) -> None:
    await _send_stored(send, _error_response(status, code, message))


def _error_response(status: int, code: str, message: str) -> StoredResponse:
    body = json.dumps(
        {"detail": {"code": code, "message": message}}, separators=(",", ":")
    ).encode()
    return StoredResponse(
        status=status,
        headers=(
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ),
        body=body,
    )
