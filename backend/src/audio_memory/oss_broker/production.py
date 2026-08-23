from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any, Mapping, Protocol

from .app import BrokerInstallation, ObjectRegistration, create_broker_app


class HashedTokenAuthorizer:
    def __init__(
        self,
        *,
        installation_id: str,
        token_sha256: str,
        daily_bytes: int,
    ) -> None:
        if len(token_sha256) != 64:
            raise ValueError("token hash must be SHA-256 hex")
        self._installation = BrokerInstallation(installation_id, daily_bytes)
        self._token_digest = bytes.fromhex(token_sha256)
        self._used_bytes = 0

    def authorize(self, token: bytes, requested_bytes: int) -> BrokerInstallation:
        supplied = hashlib.sha256(token).digest()
        if not hmac.compare_digest(supplied, self._token_digest):
            raise PermissionError("invalid installation")
        if self._used_bytes + requested_bytes > self._installation.daily_bytes:
            raise OverflowError("installation quota exceeded")
        self._used_bytes += requested_bytes
        return self._installation


class OssRequestFactory(Protocol):
    @staticmethod
    def put(*, bucket: str, key: str, content_type: str) -> object: ...

    @staticmethod
    def get(*, bucket: str, key: str) -> object: ...

    @staticmethod
    def delete(*, bucket: str, key: str) -> object: ...


class AlibabaOssSigner:
    def __init__(self, *, client: Any, bucket: str, requests: OssRequestFactory) -> None:
        self._client = client
        self._bucket = bucket
        self._requests = requests

    async def issue_upload(
        self, *, object_key: str, content_type: str, size_bytes: int, sha256: str
    ) -> dict[str, object]:
        request = self._requests.put(
            bucket=self._bucket, key=object_key, content_type=content_type
        )
        result = self._client.presign(request, expires=timedelta(minutes=15))
        return {
            "url": result.url,
            "headers": dict(result.signed_headers),
            "expires_at": result.expiration,
        }

    async def issue_read(self, *, object_key: str) -> dict[str, object]:
        request = self._requests.get(bucket=self._bucket, key=object_key)
        result = self._client.presign(request, expires=timedelta(hours=6))
        return {"url": result.url, "expires_at": result.expiration}

    async def delete(self, *, object_key: str) -> None:
        request = self._requests.delete(bucket=self._bucket, key=object_key)
        self._client.delete_object(request)


class AlibabaRequestFactory:
    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def put(self, *, bucket: str, key: str, content_type: str) -> object:
        return self._sdk.PutObjectRequest(
            bucket=bucket, key=key, content_type=content_type
        )

    def get(self, *, bucket: str, key: str) -> object:
        return self._sdk.GetObjectRequest(bucket=bucket, key=key)

    def delete(self, *, bucket: str, key: str) -> object:
        return self._sdk.DeleteObjectRequest(bucket=bucket, key=key)


def build_app_from_environment(env: Mapping[str, str], *, sdk: Any) -> Any:
    required = (
        "OSS_BUCKET",
        "OSS_REGION",
        "BROKER_TOKEN_SHA256",
        "BROKER_REGISTRY_SECRET",
    )
    missing = next((name for name in required if not env.get(name)), None)
    if missing is not None:
        raise ValueError(f"missing {missing}")

    def load_credentials() -> Any:
        return sdk.credentials.Credentials(
            access_key_id=env["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=env["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=env.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""),
        )

    cfg = sdk.config.load_default()
    cfg.region = env["OSS_REGION"]
    cfg.credentials_provider = sdk.credentials.CredentialsProviderFunc(
        func=load_credentials
    )
    signer = AlibabaOssSigner(
        client=sdk.Client(cfg),
        bucket=env["OSS_BUCKET"],
        requests=AlibabaRequestFactory(sdk),
    )
    return create_broker_app(
        authorizer=HashedTokenAuthorizer(
            installation_id=env.get("BROKER_INSTALLATION_ID", "beta6"),
            token_sha256=env["BROKER_TOKEN_SHA256"],
            daily_bytes=int(env.get("BROKER_DAILY_BYTES", str(20 * 1024**3))),
        ),
        registry=SignedObjectRegistry(
            secret=env["BROKER_REGISTRY_SECRET"].encode("utf-8")
        ),
        signer=signer,
    )


class SignedObjectRegistry:
    """Stateless ownership registry suitable for horizontally scaled functions."""

    def __init__(self, *, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("registry secret must be at least 16 bytes")
        self._secret = secret

    def register(self, registration: ObjectRegistration) -> ObjectRegistration:
        if not registration.object_key.startswith("temporary/"):
            raise ValueError("object key must use temporary prefix")
        object_id = self.object_id_for(registration)
        return ObjectRegistration(
            object_id=object_id,
            installation_id=registration.installation_id,
            object_key=registration.object_key,
        )

    def object_id_for(self, registration: ObjectRegistration) -> str:
        payload = json.dumps(
            {"i": registration.installation_id, "k": registration.object_key},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return "obj_" + self._encode(payload + signature)

    def owned(
        self, object_id: str, installation_id: str
    ) -> ObjectRegistration | None:
        if not object_id.startswith("obj_"):
            return None
        try:
            raw = self._decode(object_id.removeprefix("obj_"))
            payload, supplied_signature = raw[:-32], raw[-32:]
            expected_signature = hmac.new(
                self._secret, payload, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            values = json.loads(payload)
            if values["i"] != installation_id:
                return None
            object_key = str(values["k"])
            if not object_key.startswith("temporary/"):
                return None
            return ObjectRegistration(
                object_id=object_id,
                installation_id=installation_id,
                object_key=object_key,
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def remove(self, object_id: str, installation_id: str) -> None:
        # Ownership is encoded in the signed identifier. Deletion revokes the
        # underlying object; there is no mutable registry record to remove.
        return None

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
