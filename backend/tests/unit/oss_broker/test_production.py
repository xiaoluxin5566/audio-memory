from __future__ import annotations

import pytest
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from audio_memory.oss_broker.app import ObjectRegistration
from audio_memory.oss_broker.production import (
    AlibabaOssSigner,
    HashedTokenAuthorizer,
    SignedObjectRegistry,
    build_app_from_environment,
)


def test_broker_runtime_sources_are_importable_on_python_3_10() -> None:
    package = Path(__file__).parents[3] / "src/audio_memory/oss_broker"
    for name in ("app.py", "device_auth.py", "security_ledger.py"):
        source = (package / name).read_text(encoding="utf-8")
        assert "from datetime import UTC" not in source
        assert "datetime.UTC" not in source


class _PresignResult:
    method = "PUT"
    url = "https://signed.example/upload"
    expiration = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
    signed_headers = {"content-type": "audio/mpeg"}


class _OssClient:
    def __init__(self) -> None:
        self.presigned: list[tuple[object, object]] = []
        self.deleted: list[object] = []

    def presign(self, request: object, *, expires: object) -> _PresignResult:
        self.presigned.append((request, expires))
        return _PresignResult()

    def delete_object(self, request: object) -> None:
        self.deleted.append(request)


class _Credentials:
    access_key_id = "test-access-key"
    access_key_secret = "test-secret"
    security_token = "test-security-token"


class _Requests:
    @staticmethod
    def put(
        *, bucket: str, key: str, content_type: str, content_length: int,
        content_md5: str, metadata: dict[str, str],
    ) -> tuple[object, ...]:
        return (
            "put", bucket, key, content_type, content_length, content_md5, metadata,
        )

    @staticmethod
    def get(*, bucket: str, key: str) -> tuple[str, ...]:
        return ("get", bucket, key)

    @staticmethod
    def delete(*, bucket: str, key: str) -> tuple[str, ...]:
        return ("delete", bucket, key)


@pytest.mark.asyncio
async def test_oss_signer_scopes_upload_to_configured_bucket_and_key() -> None:
    client = _OssClient()
    signer = AlibabaOssSigner(
        client=client,
        bucket="alwaysondemo",
        requests=_Requests,
        credentials_loader=lambda: _Credentials(),
        region="cn-beijing",
        endpoint="https://alwaysondemo.oss-cn-beijing.aliyuncs.com",
    )

    signed = await signer.issue_upload(
        object_key="temporary/object-a",
        content_type="audio/mpeg",
        size_bytes=123,
        sha256="a" * 64,
    )

    policy = json.loads(base64.b64decode(signed["fields"]["policy"]))
    assert signed["method"] == "POST"
    assert signed["url"] == "https://alwaysondemo.oss-cn-beijing.aliyuncs.com"
    assert signed["fields"]["key"] == "temporary/object-a"
    assert signed["fields"]["OSSAccessKeyId"] == "test-access-key"
    assert signed["fields"]["x-oss-security-token"] == "test-security-token"
    assert ["content-length-range", 123, 123] in policy["conditions"]
    assert {"Content-Type": "audio/mpeg"} in policy["conditions"]
    assert {"x-oss-meta-sha256": "a" * 64} in policy["conditions"]


@pytest.mark.asyncio
async def test_oss_signer_deletes_only_configured_bucket_key() -> None:
    client = _OssClient()
    signer = AlibabaOssSigner(
        client=client,
        bucket="alwaysondemo",
        requests=_Requests,
        credentials_loader=lambda: _Credentials(),
        region="cn-beijing",
        endpoint="https://alwaysondemo.oss-cn-beijing.aliyuncs.com",
    )

    await signer.delete(object_key="temporary/object-a")

    assert client.deleted == [("delete", "alwaysondemo", "temporary/object-a")]


def test_hashed_token_authorizer_accepts_token_without_storing_plaintext() -> None:
    token = b"beta-installation-secret"
    authorizer = HashedTokenAuthorizer(
        installation_id="beta-installation",
        token_sha256=__import__("hashlib").sha256(token).hexdigest(),
        daily_bytes=1_000,
    )

    installation = authorizer.authorize(token, 600)

    assert installation.installation_id == "beta-installation"
    assert installation.daily_bytes == 1_000


def test_hashed_token_authorizer_rejects_wrong_token() -> None:
    authorizer = HashedTokenAuthorizer(
        installation_id="beta-installation",
        token_sha256="0" * 64,
        daily_bytes=1_000,
    )

    with pytest.raises(PermissionError):
        authorizer.authorize(b"wrong", 1)


def test_production_app_requires_private_bucket_and_device_credential_secret() -> None:
    with pytest.raises(ValueError, match="BROKER_CREDENTIAL_SECRET"):
        build_app_from_environment(
            {
                "OSS_BUCKET": "alwaysondemo",
                "OSS_REGION": "cn-beijing",
                "BROKER_REGISTRY_SECRET": "x" * 32,
            },
            sdk=object(),
        )


def test_production_app_requires_persistent_security_ledger() -> None:
    with pytest.raises(ValueError, match="BROKER_SECURITY_DB"):
        build_app_from_environment(
            {
                "OSS_BUCKET": "alwaysondemo",
                "OSS_REGION": "cn-beijing",
                "BROKER_CREDENTIAL_SECRET": "c" * 32,
                "BROKER_REGISTRY_SECRET": "r" * 32,
            },
            sdk=object(),
        )


def test_production_rejects_ephemeral_security_ledger_path() -> None:
    with pytest.raises(ValueError, match="persistent volume"):
        build_app_from_environment(
            {
                "OSS_BUCKET": "alwaysondemo",
                "OSS_REGION": "cn-beijing",
                "BROKER_CREDENTIAL_SECRET": "c" * 32,
                "BROKER_REGISTRY_SECRET": "r" * 32,
                "BROKER_SECURITY_DB": "/tmp/broker.sqlite3",
            },
            sdk=object(),
        )


def test_signed_registry_round_trips_owned_object_without_process_memory() -> None:
    first = SignedObjectRegistry(secret=b"registry-secret-32-bytes-long!!")
    registration = ObjectRegistration(
        object_id="ignored",
        installation_id="install-a",
        object_key="temporary/random-object",
    )

    first.register(registration)
    object_id = first.object_id_for(registration)
    restarted = SignedObjectRegistry(secret=b"registry-secret-32-bytes-long!!")

    assert restarted.owned(object_id, "install-a") == ObjectRegistration(
        object_id=object_id,
        installation_id="install-a",
        object_key="temporary/random-object",
    )


def test_signed_registry_rejects_cross_installation_access() -> None:
    registry = SignedObjectRegistry(secret=b"registry-secret-32-bytes-long!!")
    registration = ObjectRegistration(
        object_id="ignored",
        installation_id="install-a",
        object_key="temporary/random-object",
    )
    registry.register(registration)

    assert registry.owned(registry.object_id_for(registration), "install-b") is None


def test_signed_registry_rejects_tampered_object_id() -> None:
    registry = SignedObjectRegistry(secret=b"registry-secret-32-bytes-long!!")
    registration = ObjectRegistration(
        object_id="ignored",
        installation_id="install-a",
        object_key="temporary/random-object",
    )
    registry.register(registration)
    object_id = registry.object_id_for(registration)

    assert registry.owned(object_id[:-1] + "x", "install-a") is None


def test_signed_registry_only_accepts_temporary_prefix() -> None:
    registry = SignedObjectRegistry(secret=b"registry-secret-32-bytes-long!!")

    with pytest.raises(ValueError, match="temporary prefix"):
        registry.register(
            ObjectRegistration(
                object_id="ignored",
                installation_id="install-a",
                object_key="other/private-object",
            )
        )
