from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import ipaddress
import secrets
from typing import Literal, Mapping, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


MAX_OBJECT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BrokerInstallation:
    installation_id: str
    daily_bytes: int


class TokenAuthorizer(Protocol):
    def authorize(self, token: bytes, requested_bytes: int) -> BrokerInstallation: ...


class SignedRequestAuthorizer(Protocol):
    def authorize(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        requested_bytes: int,
    ) -> BrokerInstallation: ...


class CredentialIssuer(Protocol):
    def issue(
        self,
        *,
        public_key: bytes,
        release: str,
        daily_bytes: int,
        now: datetime | None = None,
    ) -> str: ...


class EnrollmentLimiter(Protocol):
    def claim(self, *, source: str, public_key: bytes) -> None: ...


class MemoryEnrollmentLimiter:
    def __init__(self, *, max_per_source_per_hour: int = 20) -> None:
        if max_per_source_per_hour <= 0:
            raise ValueError("enrollment limit must be positive")
        self._limit = max_per_source_per_hour
        self._counts: dict[tuple[str, str], int] = {}
        self._known: set[tuple[str, bytes]] = set()

    def claim(self, *, source: str, public_key: bytes) -> None:
        known = (source, hashlib.sha256(public_key).digest())
        if known in self._known:
            return
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        counter = (source, hour)
        used = self._counts.get(counter, 0)
        if used >= self._limit:
            raise OverflowError("enrollment rate exceeded")
        self._known.add(known)
        self._counts[counter] = used + 1


class OssSigner(Protocol):
    async def issue_upload(
        self, *, object_key: str, content_type: str, size_bytes: int, sha256: str
    ) -> dict[str, object]: ...

    async def issue_read(self, *, object_key: str) -> dict[str, object]: ...

    async def delete(self, *, object_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ObjectRegistration:
    object_id: str
    installation_id: str
    object_key: str


class ObjectRegistry(Protocol):
    def register(self, registration: ObjectRegistration) -> ObjectRegistration: ...

    def owned(self, object_id: str, installation_id: str) -> ObjectRegistration | None: ...

    def remove(self, object_id: str, installation_id: str) -> None: ...


class StaticTokenAuthorizer:
    """Development adapter; production replaces it with a durable token store."""

    def __init__(self, values: dict[bytes, BrokerInstallation]) -> None:
        self._values = {
            hashlib.sha256(token).digest(): installation
            for token, installation in values.items()
        }
        self._used_bytes: dict[str, int] = {}

    def authorize(self, token: bytes, requested_bytes: int) -> BrokerInstallation:
        digest = hashlib.sha256(token).digest()
        installation = next(
            (
                value
                for candidate, value in self._values.items()
                if hmac.compare_digest(candidate, digest)
            ),
            None,
        )
        if installation is None:
            raise PermissionError("invalid installation")
        used = self._used_bytes.get(installation.installation_id, 0)
        if used + requested_bytes > installation.daily_bytes:
            raise OverflowError("installation quota exceeded")
        self._used_bytes[installation.installation_id] = used + requested_bytes
        return installation


class MemoryObjectRegistry:
    def __init__(self) -> None:
        self._objects: dict[str, ObjectRegistration] = {}

    def register(self, registration: ObjectRegistration) -> ObjectRegistration:
        self._objects[registration.object_id] = registration
        return registration

    def owned(
        self, object_id: str, installation_id: str
    ) -> ObjectRegistration | None:
        registration = self._objects.get(object_id)
        if registration is None or registration.installation_id != installation_id:
            return None
        return registration

    def remove(self, object_id: str, installation_id: str) -> None:
        if self.owned(object_id, installation_id) is not None:
            self._objects.pop(object_id, None)


class UploadInput(BaseModel):
    content_type: Literal["audio/mpeg", "audio/aac"]
    size_bytes: int = Field(gt=0, lt=MAX_OBJECT_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UploadOutput(BaseModel):
    object_id: str
    upload_url: str
    upload_headers: dict[str, str]
    upload_method: Literal["PUT", "POST"] = "PUT"
    upload_fields: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime


class ReadOutput(BaseModel):
    url: str
    expires_at: datetime


class InstallationInput(BaseModel):
    public_key: str = Field(min_length=42, max_length=44, pattern=r"^[A-Za-z0-9_-]+$")
    release: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class InstallationOutput(BaseModel):
    credential: str


def create_broker_app(
    *,
    authorizer: TokenAuthorizer | None,
    registry: ObjectRegistry,
    signer: OssSigner,
    device_authorizer: SignedRequestAuthorizer | None = None,
    credential_authority: CredentialIssuer | None = None,
    daily_bytes: int = 20 * 1024**3,
    enrollment_limiter: EnrollmentLimiter | None = None,
    trust_forwarded_client: bool = False,
) -> FastAPI:
    app = FastAPI(title="Audio Memory OSS Authorization")

    if authorizer is None and device_authorizer is None:
        raise ValueError("an installation authorizer is required")
    if credential_authority is not None and device_authorizer is None:
        raise ValueError("device authorization is required for enrollment")

    async def authorize(
        request: Request,
        header: str | None,
        requested_bytes: int = 0,
    ) -> BrokerInstallation:
        if device_authorizer is not None and request.headers.get(
            "X-Audio-Memory-Credential"
        ):
            try:
                return device_authorizer.authorize(
                    method=request.method,
                    path=request.url.path,
                    body=await request.body(),
                    headers=request.headers,
                    requested_bytes=requested_bytes,
                )
            except PermissionError as exc:
                raise HTTPException(
                    status_code=401, detail={"code": "unauthorized"}
                ) from exc
            except OverflowError as exc:
                raise HTTPException(
                    status_code=429,
                    detail={"code": "installation_quota_exceeded"},
                ) from exc
        if authorizer is None:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        if header is None or not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        token = header.removeprefix("Bearer ").encode("utf-8")
        if not token:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        try:
            return authorizer.authorize(token, requested_bytes)
        except PermissionError as exc:
            raise HTTPException(
                status_code=401, detail={"code": "unauthorized"}
            ) from exc
        except OverflowError as exc:
            raise HTTPException(
                status_code=429, detail={"code": "installation_quota_exceeded"}
            ) from exc

    if credential_authority is not None:

        @app.post(
            "/v1/installations",
            response_model=InstallationOutput,
            status_code=201,
        )
        async def create_installation(
            payload: InstallationInput, request: Request
        ) -> InstallationOutput:
            try:
                public_key = base64.urlsafe_b64decode(
                    payload.public_key + "=" * (-len(payload.public_key) % 4)
                )
                if enrollment_limiter is not None:
                    source = request.client.host if request.client is not None else "unknown"
                    if trust_forwarded_client:
                        forwarded = request.headers.get("X-Forwarded-For", "")
                        if forwarded:
                            try:
                                # Function Compute documents the first XFF entry
                                # as the original client when proxies are present.
                                source = str(ipaddress.ip_address(
                                    forwarded.split(",", 1)[0].strip()
                                ))
                            except ValueError as exc:
                                raise HTTPException(
                                    status_code=400,
                                    detail={"code": "invalid_forwarded_client"},
                                ) from exc
                    enrollment_limiter.claim(source=source, public_key=public_key)
                credential = credential_authority.issue(
                    public_key=public_key,
                    release=payload.release,
                    daily_bytes=daily_bytes,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail={"code": "invalid_device_key"}
                ) from exc
            except OverflowError as exc:
                raise HTTPException(
                    status_code=429, detail={"code": "enrollment_rate_limited"}
                ) from exc
            return InstallationOutput(credential=credential)

        @app.post("/v1/installations/verify", status_code=204)
        async def verify_installation(
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> None:
            await authorize(request, authorization)

    @app.post("/v1/uploads", response_model=UploadOutput)
    async def create_upload(
        payload: UploadInput,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> UploadOutput:
        installation = await authorize(request, authorization, payload.size_bytes)
        object_id = f"obj_{secrets.token_urlsafe(18)}"
        object_key = f"temporary/{secrets.token_urlsafe(24)}"
        signed = await signer.issue_upload(
            object_key=object_key,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            sha256=payload.sha256,
        )
        registration = registry.register(
            ObjectRegistration(
                object_id=object_id,
                installation_id=installation.installation_id,
                object_key=object_key,
            )
        )
        return UploadOutput(
            object_id=registration.object_id,
            upload_url=str(signed["url"]),
            upload_headers=dict(signed["headers"]),  # type: ignore[arg-type]
            upload_method=str(signed.get("method", "PUT")),  # type: ignore[arg-type]
            upload_fields=dict(signed.get("fields", {})),  # type: ignore[arg-type]
            expires_at=signed["expires_at"],  # type: ignore[arg-type]
        )

    @app.post("/v1/objects/{object_id}/read-url", response_model=ReadOutput)
    async def create_read_url(
        object_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> ReadOutput:
        installation = await authorize(request, authorization)
        registration = registry.owned(object_id, installation.installation_id)
        if registration is None:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        signed = await signer.issue_read(object_key=registration.object_key)
        return ReadOutput(
            url=str(signed["url"]),
            expires_at=signed["expires_at"],  # type: ignore[arg-type]
        )

    @app.delete("/v1/objects/{object_id}", status_code=204)
    async def delete_object(
        object_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        installation = await authorize(request, authorization)
        registration = registry.owned(object_id, installation.installation_id)
        if registration is None:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        await signer.delete(object_key=registration.object_key)
        registry.remove(object_id, installation.installation_id)

    return app
