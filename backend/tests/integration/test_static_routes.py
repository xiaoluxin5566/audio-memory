from pathlib import Path

import httpx
import pytest

from audio_memory.config import AppPaths
from audio_memory.main import create_app


@pytest.mark.asyncio
async def test_built_frontend_serves_all_product_routes_without_capturing_api(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<main>Audio Memory</main>", encoding="utf-8")
    (frontend / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    app = create_app(paths=AppPaths.from_home(tmp_path), frontend_dir=frontend)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8765"
    ) as client:
        root = await client.get("/")
        history = await client.get("/history")
        prompts = await client.get("/settings/prompts")
        asset = await client.get("/assets/app.js")
        missing_api = await client.get("/api/does-not-exist")

    assert root.status_code == history.status_code == prompts.status_code == 200
    assert "Audio Memory" in root.text
    assert asset.text == "console.log('ok')"
    assert missing_api.status_code == 404
