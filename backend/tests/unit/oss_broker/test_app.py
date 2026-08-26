from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from audio_memory.oss_broker.app import (
    BrokerInstallation,
    MemoryObjectRegistry,
    StaticTokenAuthorizer,
    create_broker_app,
)


class Signer:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, int, str]] = []
        self.reads: list[str] = []
        self.deletes: list[str] = []

    async def issue_upload(
        self, *, object_key: str, content_type: str, size_bytes: int, sha256: str,
    ):
        self.uploads.append((object_key, content_type, size_bytes, sha256))
        return {
            "url": "https://bucket.oss-cn-beijing.aliyuncs.com/object?sig=upload",
            "headers": {
                "Content-Type": content_type,
                "x-oss-meta-sha256": sha256,
            },
            "expires_at": datetime(2026, 8, 24, tzinfo=UTC),
        }

    async def issue_read(self, *, object_key: str):
        self.reads.append(object_key)
        return {
            "url": "https://bucket.oss-cn-beijing.aliyuncs.com/object?sig=read",
            "expires_at": datetime(2026, 8, 24, tzinfo=UTC),
        }

    async def delete(self, *, object_key: str) -> None:
        self.deletes.append(object_key)


@pytest.fixture
def broker():
    signer = Signer()
    app = create_broker_app(
        authorizer=StaticTokenAuthorizer(
            {
                b"install-a": BrokerInstallation("installation-a", daily_bytes=1024),
                b"install-b": BrokerInstallation("installation-b", daily_bytes=1024),
            }
        ),
        registry=MemoryObjectRegistry(),
        signer=signer,
    )
    return app, signer


@pytest.mark.asyncio
async def test_upload_ticket_is_single_object_and_filename_free(broker) -> None:
    app, signer = broker
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        response = await client.post(
            "/v1/uploads",
            headers={"Authorization": "Bearer install-a"},
            json={
                "content_type": "audio/mpeg",
                "size_bytes": 100,
                "sha256": "a" * 64,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "object_id", "upload_url", "upload_headers", "upload_method",
        "upload_fields", "expires_at",
    }
    assert "installation-a" not in body["object_id"]
    object_key, content_type, size_bytes, sha256 = signer.uploads[0]
    assert object_key.startswith("temporary/")
    assert "installation-a" not in object_key
    assert (content_type, size_bytes, sha256) == ("audio/mpeg", 100, "a" * 64)


@pytest.mark.asyncio
async def test_other_installation_cannot_read_or_delete_object(broker) -> None:
    app, signer = broker
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        created = await client.post(
            "/v1/uploads",
            headers={"Authorization": "Bearer install-a"},
            json={
                "content_type": "audio/aac",
                "size_bytes": 100,
                "sha256": "b" * 64,
            },
        )
        object_id = created.json()["object_id"]
        read = await client.post(
            f"/v1/objects/{object_id}/read-url",
            headers={"Authorization": "Bearer install-b"},
        )
        deleted = await client.delete(
            f"/v1/objects/{object_id}",
            headers={"Authorization": "Bearer install-b"},
        )

    assert read.status_code == 404
    assert deleted.status_code == 404
    assert signer.reads == []
    assert signer.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}],
)
async def test_missing_or_invalid_installation_token_is_rejected(broker, headers) -> None:
    app, signer = broker
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        response = await client.post(
            "/v1/uploads",
            headers=headers,
            json={
                "content_type": "audio/mpeg",
                "size_bytes": 100,
                "sha256": "a" * 64,
            },
        )
    assert response.status_code == 401
    assert signer.uploads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"content_type": "audio/wav", "size_bytes": 100, "sha256": "a" * 64},
        {"content_type": "audio/mpeg", "size_bytes": 0, "sha256": "a" * 64},
        {
            "content_type": "audio/mpeg",
            "size_bytes": 512 * 1024 * 1024,
            "sha256": "a" * 64,
        },
        {"content_type": "audio/mpeg", "size_bytes": 100, "sha256": "bad"},
    ],
)
async def test_invalid_upload_scope_is_rejected_before_signing(broker, payload) -> None:
    app, signer = broker
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        response = await client.post(
            "/v1/uploads",
            headers={"Authorization": "Bearer install-a"},
            json=payload,
        )
    assert response.status_code == 422
    assert signer.uploads == []
