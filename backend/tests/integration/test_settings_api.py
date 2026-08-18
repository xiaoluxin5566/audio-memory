from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from audio_memory.api.settings import router
from audio_memory.db import Database
from audio_memory.repositories import AppSettingsRepository


@pytest.mark.asyncio
async def test_sleep_prevention_setting_defaults_off_and_persists(tmp_path: Path) -> None:
    database = Database(tmp_path / "settings.sqlite3")
    await database.create_schema()
    app = FastAPI()
    app.state.settings_repository = AppSettingsRepository(database)
    app.state.sleep_prevention = type(
        "SleepState", (), {"status": "inactive"}
    )()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = await client.get("/api/settings/analysis")
        enabled = await client.put(
            "/api/settings/analysis", json={"prevent_sleep": True}
        )
        persisted = await client.get("/api/settings/analysis")

    assert initial.status_code == 200
    assert initial.json() == {
        "prevent_sleep": False,
        "sleep_prevention_status": "inactive",
    }
    assert enabled.status_code == 200
    assert enabled.json()["prevent_sleep"] is True
    assert persisted.json()["prevent_sleep"] is True
    await database.dispose()


@pytest.mark.asyncio
async def test_settings_api_reports_unavailable_system_protection(tmp_path: Path) -> None:
    database = Database(tmp_path / "settings.sqlite3")
    await database.create_schema()
    app = FastAPI()
    app.state.settings_repository = AppSettingsRepository(database)
    app.state.sleep_prevention = type(
        "SleepState", (), {"status": "unavailable"}
    )()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/settings/analysis")

    assert response.json()["sleep_prevention_status"] == "unavailable"
    await database.dispose()
