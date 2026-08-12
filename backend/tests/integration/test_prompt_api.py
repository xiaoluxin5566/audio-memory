from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from audio_memory.api.prompts import router
from audio_memory.prompts.store import PromptStore


@pytest.mark.asyncio
async def test_prompt_api_lists_only_runtime_autonomous_prompts(tmp_path: Path) -> None:
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

    prompts = listed.json()["prompts"]
    assert [item["scene_id"] for item in prompts] == [
        "autonomous-analysis",
        "autonomous-profile",
    ]
    assert [item["label"] for item in prompts] == ["自主分析", "隐藏画像"]
    assert all(item["editable"] is False for item in prompts)
    assert "高级个人分析顾问" in prompts[0]["content"]
    assert "隐藏用户画像" in prompts[1]["content"]
    # The old fixed-scene endpoint remains writable only as a compatibility layer.
    assert saved.json()["version"] == 2
    assert stale.status_code == 409
    assert unknown.status_code == 422
