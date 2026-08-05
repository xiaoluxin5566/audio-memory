from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from audio_memory.api.content import router
from audio_memory.config import AppPaths
from audio_memory.content.service import ContentService
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, Batch, Card, JobFile, Todo, Transcript


class FakeQuestionAnswerer:
    async def answer(self, **kwargs):
        assert "会议原文" in kwargs["transcript"]
        return "本次会议决定先做 macOS。"


@pytest_asyncio.fixture
async def content_client(tmp_path: Path):
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    job_id, batch_id, card_id, todo_id, file_id = [str(uuid4()) for _ in range(5)]
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage="completed", provider_id="kimi", model_id="kimi-k2.5"))
        session.add(Batch(id=batch_id, job_id=job_id, natural_date="2026-08-05", uploaded_at="2026-08-05T10:00:00+00:00"))
        session.add(JobFile(id=file_id, job_id=job_id, original_name="会议.mp3", extension=".mp3", size_bytes=10, sha256="c" * 64, duration_ms=1000, position=0, temporary_path=str(paths.audio / "会议.mp3")))
        session.add(Transcript(id=str(uuid4()), job_file_id=file_id, segment_index=0, start_ms=0, end_ms=1000, text="会议原文", words_json="[]"))
        session.add(Card(id=card_id, batch_id=batch_id, scene_id="meeting", position=0, payload_json=json.dumps({"card": {"title": "评审会", "summary": "确认一期范围"}, "detail_sections": []}, ensure_ascii=False)))
        session.add(Todo(id=todo_id, batch_id=batch_id, text="已过期事项", due_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()))
        await session.commit()
    app = FastAPI()
    app.state.content_service = ContentService(database, paths, FakeQuestionAnswerer())
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, paths, database, {"card_id": card_id, "todo_id": todo_id}
    await database.dispose()


@pytest.mark.asyncio
async def test_feed_history_todo_and_scoped_question(content_client):
    client, _, _, ids = content_client
    feed = (await client.get("/api/feed")).json()
    history = (await client.get("/api/history")).json()
    answer = await client.post(f"/api/cards/{ids['card_id']}/questions", json={"question": "决定是什么？"})
    refreshed_feed = (await client.get("/api/feed")).json()

    assert feed["todos"][0]["completed"] is True
    assert feed["days"][0]["cards"][0]["scene_id"] == "meeting"
    assert history["days"][0]["audio"][0]["original_name"] == "会议.mp3"
    assert answer.json()["messages"][-1]["role"] == "assistant"
    assert refreshed_feed["days"][0]["cards"][0]["qa"] == answer.json()["messages"]


@pytest.mark.asyncio
async def test_todo_supports_edit_complete_and_delete(content_client):
    client, _, _, ids = content_client
    changed = await client.patch(f"/api/todos/{ids['todo_id']}", json={"text": "修改后", "completed": True})
    removed = await client.delete(f"/api/todos/{ids['todo_id']}")

    assert changed.json()["text"] == "修改后"
    assert changed.json()["completed"] is True
    assert removed.status_code == 204
