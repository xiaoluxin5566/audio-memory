from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .app import BrokerInstallation, ObjectRegistration, create_broker_app
from .device_auth import (
    CredentialAuthority,
    DeviceRequestAuthorizer,
)
from .security_ledger import SqliteEnrollmentLimiter, SqliteSecurityLedger


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
    def put(
        *, bucket: str, key: str, content_type: str, content_length: int,
        content_md5: str, metadata: dict[str, str],
    ) -> object: ...

    @staticmethod
    def get(*, bucket: str, key: str) -> object: ...

    @staticmethod
    def delete(*, bucket: str, key: str) -> object: ...


class AlibabaOssSigner:
    def __init__(
        self, *, client: Any, bucket: str, requests: OssRequestFactory,
        credentials_loader: Any, region: str, endpoint: str,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._requests = requests
        self._credentials_loader = credentials_loader
        self._region = region
        self._endpoint = endpoint.rstrip("/")

    async def issue_upload(
        self, *, object_key: str, content_type: str, size_bytes: int, sha256: str
    ) -> dict[str, object]:
        credentials = self._credentials_loader()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        conditions: list[object] = [
            {"bucket": self._bucket},
            {"key": object_key},
            {"Content-Type": content_type},
            {"x-oss-meta-sha256": sha256},
            {"success_action_status": "200"},
            ["content-length-range", size_bytes, size_bytes],
        ]
        fields = {
            "key": object_key,
            "Content-Type": content_type,
            "x-oss-meta-sha256": sha256,
            "success_action_status": "200",
            "OSSAccessKeyId": credentials.access_key_id,
        }
        if credentials.security_token:
            conditions.append({"x-oss-security-token": credentials.security_token})
            fields["x-oss-security-token"] = credentials.security_token
        policy = base64.b64encode(json.dumps(
            {
                "expiration": expires_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "conditions": conditions,
            },
            separators=(",", ":"),
        ).encode("utf-8")).decode("ascii")
        fields["policy"] = policy
        fields["Signature"] = base64.b64encode(hmac.new(
            credentials.access_key_secret.encode("utf-8"),
            policy.encode("ascii"),
            hashlib.sha1,
        ).digest()).decode("ascii")
        return {
            "url": self._endpoint,
            "method": "POST",
            "fields": fields,
            "headers": {},
            "expires_at": expires_at,
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

    def put(
        self, *, bucket: str, key: str, content_type: str, content_length: int,
        content_md5: str, metadata: dict[str, str],
    ) -> object:
        return self._sdk.PutObjectRequest(
            bucket=bucket,
            key=key,
            content_type=content_type,
            content_length=content_length,
            content_md5=content_md5,
            metadata=metadata,
        )

    def get(self, *, bucket: str, key: str) -> object:
        return self._sdk.GetObjectRequest(bucket=bucket, key=key)

    def delete(self, *, bucket: str, key: str) -> object:
        return self._sdk.DeleteObjectRequest(bucket=bucket, key=key)


def build_app_from_environment(env: Mapping[str, str], *, sdk: Any) -> Any:
    required = (
        "OSS_BUCKET",
        "OSS_REGION",
        "BROKER_CREDENTIAL_SECRET",
        "BROKER_REGISTRY_SECRET",
        "BROKER_SECURITY_DB",
    )
    missing = next((name for name in required if not env.get(name)), None)
    if missing is not None:
        raise ValueError(f"missing {missing}")
    security_db = Path(env["BROKER_SECURITY_DB"])
    try:
        security_db.resolve(strict=False).relative_to(Path("/mnt"))
    except ValueError as exc:
        raise ValueError("BROKER_SECURITY_DB must be on a /mnt persistent volume") from exc

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
        credentials_loader=load_credentials,
        region=env["OSS_REGION"],
        endpoint=env.get(
            "OSS_ENDPOINT",
            f"https://{env['OSS_BUCKET']}.oss-{env['OSS_REGION']}.aliyuncs.com",
        ),
    )
    credential_authority = CredentialAuthority(
        secret=env["BROKER_CREDENTIAL_SECRET"].encode("utf-8")
    )
    daily_bytes = int(env.get("BROKER_DAILY_BYTES", str(20 * 1024**3)))
    ledger = SqliteSecurityLedger(security_db)
    for installation_id in env.get("BROKER_REVOKED_INSTALLATIONS", "").split(","):
        if installation_id.strip():
            ledger.revoke(installation_id.strip())
    return create_broker_app(
        authorizer=None,
        device_authorizer=DeviceRequestAuthorizer(
            authority=credential_authority,
            ledger=ledger,
        ),
        credential_authority=credential_authority,
        daily_bytes=daily_bytes,
        enrollment_limiter=SqliteEnrollmentLimiter(
            ledger=ledger,
            max_per_source_per_hour=int(
                env.get("BROKER_ENROLLMENTS_PER_SOURCE_HOUR", "20")
            ),
            max_global_per_hour=int(
                env.get("BROKER_ENROLLMENTS_GLOBAL_HOUR", "500")
            ),
            enrollment_enabled=env.get("BROKER_ENROLLMENT_ENABLED", "1") == "1",
        ),
        trust_forwarded_client=env.get("BROKER_TRUST_FORWARDED_CLIENT", "1") == "1",
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
