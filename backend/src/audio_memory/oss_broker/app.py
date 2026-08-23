from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import secrets
from typing import Literal, Protocol

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


MAX_OBJECT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BrokerInstallation:
    installation_id: str
    daily_bytes: int


class TokenAuthorizer(Protocol):
    def authorize(self, token: bytes, requested_bytes: int) -> BrokerInstallation: ...


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
    def register(self, registration: ObjectRegistration) -> None: ...

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

    def register(self, registration: ObjectRegistration) -> None:
        self._objects[registration.object_id] = registration

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
    expires_at: datetime


class ReadOutput(BaseModel):
    url: str
    expires_at: datetime


def create_broker_app(
    *, authorizer: TokenAuthorizer, registry: ObjectRegistry, signer: OssSigner
) -> FastAPI:
    app = FastAPI(title="Audio Memory OSS Authorization")

    def authorize(header: str | None, requested_bytes: int = 0) -> BrokerInstallation:
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

    @app.post("/v1/uploads", response_model=UploadOutput)
    async def create_upload(
        payload: UploadInput,
        authorization: str | None = Header(default=None),
    ) -> UploadOutput:
        installation = authorize(authorization, payload.size_bytes)
        object_id = f"obj_{secrets.token_urlsafe(18)}"
        object_key = f"temporary/{secrets.token_urlsafe(24)}"
        signed = await signer.issue_upload(
            object_key=object_key,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            sha256=payload.sha256,
        )
        registry.register(
            ObjectRegistration(
                object_id=object_id,
                installation_id=installation.installation_id,
                object_key=object_key,
            )
        )
        return UploadOutput(
            object_id=object_id,
            upload_url=str(signed["url"]),
            upload_headers=dict(signed["headers"]),  # type: ignore[arg-type]
            expires_at=signed["expires_at"],  # type: ignore[arg-type]
        )

    @app.post("/v1/objects/{object_id}/read-url", response_model=ReadOutput)
    async def create_read_url(
        object_id: str,
        authorization: str | None = Header(default=None),
    ) -> ReadOutput:
        installation = authorize(authorization)
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
        authorization: str | None = Header(default=None),
    ) -> None:
        installation = authorize(authorization)
        registration = registry.owned(object_id, installation.installation_id)
        if registration is None:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        await signer.delete(object_key=registration.object_key)
        registry.remove(object_id, installation.installation_id)

    return app

