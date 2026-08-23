from __future__ import annotations

import pytest
from datetime import datetime, timezone

from audio_memory.oss_broker.app import ObjectRegistration
from audio_memory.oss_broker.production import (
    AlibabaOssSigner,
    HashedTokenAuthorizer,
    SignedObjectRegistry,
    build_app_from_environment,
)


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


class _Requests:
    @staticmethod
    def put(*, bucket: str, key: str, content_type: str) -> tuple[str, ...]:
        return ("put", bucket, key, content_type)

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
        client=client, bucket="alwaysondemo", requests=_Requests
    )

    signed = await signer.issue_upload(
        object_key="temporary/object-a",
        content_type="audio/mpeg",
        size_bytes=123,
        sha256="a" * 64,
    )

    assert client.presigned[0][0] == (
        "put",
        "alwaysondemo",
        "temporary/object-a",
        "audio/mpeg",
    )
    assert signed == {
        "url": "https://signed.example/upload",
        "headers": {"content-type": "audio/mpeg"},
        "expires_at": datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_oss_signer_deletes_only_configured_bucket_key() -> None:
    client = _OssClient()
    signer = AlibabaOssSigner(
        client=client, bucket="alwaysondemo", requests=_Requests
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


def test_production_app_requires_private_bucket_and_hashed_secrets() -> None:
    with pytest.raises(ValueError, match="BROKER_TOKEN_SHA256"):
        build_app_from_environment(
            {
                "OSS_BUCKET": "alwaysondemo",
                "OSS_REGION": "cn-beijing",
                "BROKER_REGISTRY_SECRET": "x" * 32,
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
