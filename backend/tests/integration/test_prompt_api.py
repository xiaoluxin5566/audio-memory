from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from audio_memory.api.prompts import router
from audio_memory.prompts.store import PromptStore


@pytest.mark.asyncio
async def test_prompt_api_only_allows_edit_and_save_for_fixed_scenes(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.prompt_store = PromptStore(tmp_path)
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/prompts")
        saved = await client.put(
            "/api/prompts/growth",
            json={"expected_version": 1, "content": "新的成长建议提示词"},
        )
        stale = await client.put(
            "/api/prompts/growth",
            json={"expected_version": 1, "content": "冲突内容"},
        )
        unknown = await client.put(
            "/api/prompts/new-scene",
            json={"expected_version": 1, "content": "不能新增"},
        )

    assert len(listed.json()["prompts"]) == 6
    assert saved.json()["version"] == 2
    assert stale.status_code == 409
    assert unknown.status_code == 422
