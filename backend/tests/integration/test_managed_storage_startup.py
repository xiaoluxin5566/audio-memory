from __future__ import annotations

from pathlib import Path
import asyncio
import time

import httpx
import pytest

from audio_memory.config import RuntimeConfig
from audio_memory.main import DEFAULT_OSS_BROKER_URL, create_app
from audio_memory.providers.keychain import (
    ERR_SEC_ITEM_NOT_FOUND,
    ERR_SEC_SUCCESS,
)


class EmptyPersistentSecurityClient:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}

    def read(self, service: str, account: str):
        value = self.values.get((service, account))
        if value is None:
            return ERR_SEC_ITEM_NOT_FOUND, None
        return ERR_SEC_SUCCESS, value

    def update(self, service: str, account: str, value: bytes) -> int:
        key = (service, account)
        if key not in self.values:
            return ERR_SEC_ITEM_NOT_FOUND
        self.values[key] = value
        return ERR_SEC_SUCCESS

    def add(self, service: str, account: str, value: bytes) -> int:
        self.values[(service, account)] = value
        return ERR_SEC_SUCCESS


@pytest.mark.asyncio
async def test_fresh_install_auto_enrolls_managed_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    security = EmptyPersistentSecurityClient()
    monkeypatch.setattr("audio_memory.main.MacSecurityClient", lambda: security)
    enrollment = respx_mock.post(
        f"{DEFAULT_OSS_BROKER_URL}/v1/installations"
    ).mock(
        return_value=__import__("httpx").Response(
            201, json={"credential": "device-bound-credential"}
        )
    )
    runtime = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime)

    async with app.router.lifespan_context(app):
        readiness = await app.state.pipeline_readiness.check(
            refresh_managed_storage=True
        )
        assert readiness.managed_storage_ready is True
        assert app.state.cloud_asr_coordinator is not None

    assert enrollment.call_count == 1
    assert (
        "Audio Memory Dev",
        "managed_storage:device-key",
    ) in security.values
    assert security.values[
        ("Audio Memory Dev", "managed_storage:credential")
    ] == b"device-bound-credential"
    assert ("Audio Memory Dev", "installation:beta") not in security.values


@pytest.mark.asyncio
async def test_slow_broker_does_not_block_local_application_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    security = EmptyPersistentSecurityClient()
    monkeypatch.setattr("audio_memory.main.MacSecurityClient", lambda: security)
    release_enrollment = asyncio.Event()

    async def delayed_enrollment(_request: httpx.Request) -> httpx.Response:
        await release_enrollment.wait()
        return httpx.Response(201, json={"credential": "eventually-ready"})

    respx_mock.post(f"{DEFAULT_OSS_BROKER_URL}/v1/installations").mock(
        side_effect=delayed_enrollment
    )
    runtime = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime)
    started_at = time.monotonic()

    async with app.router.lifespan_context(app):
        assert time.monotonic() - started_at < 1.0
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8766",
        ) as client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert app.state.pipeline_readiness.managed_storage.ready is False
        release_enrollment.set()
        await app.state.managed_storage_startup_task
        assert app.state.pipeline_readiness.managed_storage.ready is True
