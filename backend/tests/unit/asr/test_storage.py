from __future__ import annotations

import httpx
import pytest

from audio_memory.asr.storage import (
    ManagedOssClient,
    StorageAuthorizationError,
    UploadTicket,
    UploadRequest,
)
from datetime import UTC, datetime


def upload_request() -> UploadRequest:
    return UploadRequest(
        content_type="audio/mpeg",
        size_bytes=1024,
        sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_broker_issues_one_object_upload_without_receiving_filename(
    respx_mock,
) -> None:
    route = respx_mock.post("https://broker.example/v1/uploads").mock(
        return_value=httpx.Response(
            200,
            json={
                "object_id": "obj_7f4c",
                "upload_url": "https://bucket.oss-cn-beijing.aliyuncs.com/random?sig=x",
                "upload_headers": {"Content-Type": "audio/mpeg"},
                "expires_at": "2026-08-23T13:00:00Z",
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        ticket = await ManagedOssClient(
            http_client=http_client,
            broker_base_url="https://broker.example",
            installation_token=b"install-secret",
        ).create_upload(upload_request())

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer install-secret"
    assert request.read().decode() == (
        '{"content_type":"audio/mpeg","size_bytes":1024,"sha256":"'
        + "a" * 64
        + '"}'
    )
    assert ticket.object_id == "obj_7f4c"
    assert "sig=x" not in repr(ticket)
    assert "install-secret" not in repr(ticket)


@pytest.mark.asyncio
async def test_read_url_and_delete_are_scoped_to_object_id(respx_mock) -> None:
    read_route = respx_mock.post(
        "https://broker.example/v1/objects/obj_7f4c/read-url"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://bucket.oss-cn-beijing.aliyuncs.com/random?sig=read",
                "expires_at": "2026-08-23T13:00:00Z",
            },
        )
    )
    delete_route = respx_mock.delete(
        "https://broker.example/v1/objects/obj_7f4c"
    ).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http_client:
        client = ManagedOssClient(
            http_client=http_client,
            broker_base_url="https://broker.example/",
            installation_token=b"install-secret",
        )
        read_ticket = await client.create_read_url("obj_7f4c")
        await client.delete("obj_7f4c")

    assert "sig=read" not in repr(read_ticket)
    assert read_route.called
    assert delete_route.called


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_broker_errors_are_stable_and_secret_free(respx_mock, status: int) -> None:
    respx_mock.post("https://broker.example/v1/uploads").mock(
        return_value=httpx.Response(status, text="do not expose provider details")
    )
    async with httpx.AsyncClient() as http_client:
        client = ManagedOssClient(
            http_client=http_client,
            broker_base_url="https://broker.example",
            installation_token=b"install-secret",
        )
        with pytest.raises(StorageAuthorizationError) as caught:
            await client.create_upload(upload_request())

    expected = {
        401: ("installation_unauthorized", False),
        403: ("installation_unauthorized", False),
        429: ("storage_rate_limited", True),
        500: ("storage_unavailable", True),
    }[status]
    assert (caught.value.code, caught.value.retriable) == expected
    assert "install-secret" not in repr(caught.value)
    assert "provider details" not in repr(caught.value)


def test_broker_must_use_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ManagedOssClient(
            http_client=httpx.AsyncClient(),
            broker_base_url="http://broker.example",
            installation_token=b"token",
        )


@pytest.mark.asyncio
async def test_upload_streams_original_file_with_ticket_headers(
    respx_mock, tmp_path
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"original-audio")
    route = respx_mock.put(
        "https://bucket.oss-cn-beijing.aliyuncs.com/random?sig=x"
    ).mock(return_value=httpx.Response(200))
    ticket = UploadTicket(
        object_id="obj_7f4c",
        upload_url="https://bucket.oss-cn-beijing.aliyuncs.com/random?sig=x",
        upload_headers={"Content-Type": "audio/mpeg", "x-oss-meta-sha256": "abc"},
        expires_at=datetime(2026, 8, 23, 13, tzinfo=UTC),
    )
    async with httpx.AsyncClient() as http_client:
        client = ManagedOssClient(
            http_client=http_client,
            broker_base_url="https://broker.example",
            installation_token=b"install-secret",
        )
        await client.upload_file(ticket, source)

    request = route.calls[0].request
    assert request.content == b"original-audio"
    assert request.headers["Content-Type"] == "audio/mpeg"
    assert request.headers["x-oss-meta-sha256"] == "abc"
