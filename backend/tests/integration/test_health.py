from pathlib import Path

import httpx
import pytest

from audio_memory.config import AppPaths
from audio_memory.main import create_app


@pytest.mark.asyncio
async def test_health_is_ready_after_application_startup(tmp_path: Path) -> None:
    app = create_app(paths=AppPaths.from_home(tmp_path))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
        ) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0-beta.1",
        "platform": "macOS",
        "architecture": "arm64",
    }


@pytest.mark.asyncio
async def test_startup_migrates_and_opens_the_local_database(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)
    app = create_app(paths=paths)

    async with app.router.lifespan_context(app):
        assert paths.database.is_file()
        assert app.state.database.path == paths.database

    assert app.state.database.engine.sync_engine.pool.checkedout() == 0
