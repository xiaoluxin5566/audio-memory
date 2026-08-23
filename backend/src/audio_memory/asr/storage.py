from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

import httpx


_OBJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")


class StorageAuthorizationError(RuntimeError):
    def __init__(self, code: str, *, retriable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retriable = retriable


@dataclass(frozen=True, slots=True)
class UploadRequest:
    content_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UploadTicket:
    object_id: str
    upload_url: str = field(repr=False)
    upload_headers: dict[str, str] = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReadTicket:
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(slots=True)
class ManagedOssClient:
    http_client: httpx.AsyncClient = field(repr=False)
    broker_base_url: str
    installation_token: bytes = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.broker_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("OSS broker must use HTTPS")
        self.broker_base_url = self.broker_base_url.rstrip("/")
        if not self.installation_token:
            raise ValueError("installation credential must not be blank")

    async def create_upload(self, request: UploadRequest) -> UploadTicket:
        response = await self._request(
            "POST",
            "/v1/uploads",
            json={
                "content_type": request.content_type,
                "size_bytes": request.size_bytes,
                "sha256": request.sha256,
            },
        )
        body = self._json_object(response)
        object_id = self._object_id(body.get("object_id"))
        upload_url = self._https_url(body.get("upload_url"))
        upload_headers = body.get("upload_headers")
        if not isinstance(upload_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in upload_headers.items()
        ):
            raise StorageAuthorizationError("storage_protocol_error", retriable=False)
        return UploadTicket(
            object_id=object_id,
            upload_url=upload_url,
            upload_headers=upload_headers,
            expires_at=self._datetime(body.get("expires_at")),
        )

    async def create_read_url(self, object_id: str) -> ReadTicket:
        safe_id = quote(self._object_id(object_id), safe="")
        response = await self._request(
            "POST", f"/v1/objects/{safe_id}/read-url", json={}
        )
        body = self._json_object(response)
        return ReadTicket(
            url=self._https_url(body.get("url")),
            expires_at=self._datetime(body.get("expires_at")),
        )

    async def upload_file(self, ticket: UploadTicket, source: Path) -> None:
        async def chunks():
            with source.open("rb") as stream:
                while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
                    yield chunk

        try:
            response = await self.http_client.put(
                ticket.upload_url,
                headers=ticket.upload_headers,
                content=chunks(),
            )
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            raise StorageAuthorizationError("storage_timeout", retriable=True) from exc
        except httpx.RequestError as exc:
            raise StorageAuthorizationError(
                "storage_unavailable", retriable=True
            ) from exc
        if response.status_code in {401, 403}:
            raise StorageAuthorizationError("upload_ticket_expired", retriable=True)
        if response.status_code == 429 or response.status_code >= 500:
            raise StorageAuthorizationError("storage_unavailable", retriable=True)
        if response.status_code >= 400:
            raise StorageAuthorizationError("storage_upload_rejected", retriable=False)

    async def delete(self, object_id: str) -> None:
        safe_id = quote(self._object_id(object_id), safe="")
        await self._request("DELETE", f"/v1/objects/{safe_id}")

    async def _request(
        self, method: str, path: str, *, json: dict[str, object] | None = None
    ) -> httpx.Response:
        try:
            token = self.installation_token.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageAuthorizationError(
                "installation_unauthorized", retriable=False
            ) from exc
        try:
            response = await self.http_client.request(
                method,
                f"{self.broker_base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=json,
            )
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            raise StorageAuthorizationError("storage_timeout", retriable=True) from exc
        except httpx.RequestError as exc:
            raise StorageAuthorizationError(
                "storage_unavailable", retriable=True
            ) from exc
        if response.status_code in {401, 403}:
            raise StorageAuthorizationError(
                "installation_unauthorized", retriable=False
            )
        if response.status_code == 429:
            raise StorageAuthorizationError("storage_rate_limited", retriable=True)
        if response.status_code >= 500:
            raise StorageAuthorizationError("storage_unavailable", retriable=True)
        if response.status_code >= 400:
            raise StorageAuthorizationError("storage_request_rejected", retriable=False)
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, object]:
        try:
            body = response.json()
        except ValueError as exc:
            raise StorageAuthorizationError(
                "storage_protocol_error", retriable=False
            ) from exc
        if not isinstance(body, dict):
            raise StorageAuthorizationError("storage_protocol_error", retriable=False)
        return body

    @staticmethod
    def _object_id(value: object) -> str:
        if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
            raise StorageAuthorizationError("storage_protocol_error", retriable=False)
        return value

    @staticmethod
    def _https_url(value: object) -> str:
        if not isinstance(value, str):
            raise StorageAuthorizationError("storage_protocol_error", retriable=False)
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise StorageAuthorizationError("storage_protocol_error", retriable=False)
        return value

    @staticmethod
    def _datetime(value: object) -> datetime:
        if not isinstance(value, str):
            raise StorageAuthorizationError("storage_protocol_error", retriable=False)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StorageAuthorizationError(
                "storage_protocol_error", retriable=False
            ) from exc
        if parsed.tzinfo is None:
            raise StorageAuthorizationError("storage_protocol_error", retriable=False)
        return parsed
