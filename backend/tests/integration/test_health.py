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
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "platform": "macOS",
        "architecture": "arm64",
    }
