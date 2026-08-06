from __future__ import annotations

import json
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
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    Todo,
    TodoTombstone,
    Transcript,
)


class FakeQuestionAnswerer:
    async def answer(self, **kwargs):
        assert "会议原文" in kwargs["transcript"]
        assert "不可信文本" not in kwargs["transcript"]
        return "本次会议决定先做 macOS。"


@pytest_asyncio.fixture
async def content_client(tmp_path: Path):
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    job_id, batch_id, card_id, todo_id, file_id = [str(uuid4()) for _ in range(5)]
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage="completed", provider_id="mutable-provider", model_id="mutable-model", prompt_snapshot_json='{"mutable":true}'))
        session.add(Batch(id=batch_id, job_id=job_id, natural_date="2026-08-05", uploaded_at="2026-08-05T10:00:00+00:00"))
        await session.flush()
        version_id = str(uuid4())
        session.add(
            AnalysisVersion(
                id=version_id,
                source_job_id=job_id,
                batch_id=batch_id,
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=3,
                prompt_snapshot_json='{"meeting":{"content":"frozen prompt"}}',
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="completed",
            )
        )
        await session.flush()
        batch = await session.get(Batch, batch_id)
        assert batch is not None
        batch.current_analysis_version_id = version_id
        session.add(JobFile(id=file_id, job_id=job_id, original_name="会议.mp3", extension=".mp3", size_bytes=10, sha256="c" * 64, duration_ms=1000, position=0, temporary_path=str(paths.audio / "会议.mp3")))
        session.add(Transcript(id=str(uuid4()), job_file_id=file_id, segment_index=0, start_ms=0, end_ms=1000, text="会议原文", words_json="[]"))
        session.add(
            Transcript(
                id=str(uuid4()),
                job_file_id=file_id,
                segment_index=1,
                start_ms=1000,
                end_ms=2000,
                text="不可信文本",
                words_json="[]",
                risk_state="HIGH_RISK_PENDING",
                is_reliable=False,
            )
        )
        session.add(Card(id=card_id, batch_id=batch_id, analysis_version_id=version_id, scene_id="meeting", position=0, payload_json=json.dumps({"card": {"title": "评审会", "summary": "确认一期范围"}, "detail_sections": []}, ensure_ascii=False)))
        session.add(Todo(id=todo_id, batch_id=batch_id, analysis_version_id=version_id, source_job_id=job_id, source_event_id="event_1", normalized_action="follow up", normalized_assignee="user", source_fingerprint="fingerprint-1", text="已过期事项", due_at="2026-08-04T08:00:00+00:00"))
        await session.commit()
    app = FastAPI()
    app.state.content_service = ContentService(database, paths, FakeQuestionAnswerer())
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, paths, database, {"batch_id": batch_id, "card_id": card_id, "todo_id": todo_id}
    await database.dispose()


@pytest.mark.asyncio
async def test_expired_todo_stays_incomplete_and_is_marked_overdue(content_client):
    client, _, _, ids = content_client
    feed = (await client.get("/api/feed")).json()

    todo = next(item for item in feed["todos"] if item["id"] == ids["todo_id"])
    assert todo["completed"] is False
    assert todo["overdue"] is True


@pytest.mark.asyncio
async def test_feed_history_and_scoped_question(content_client):
    client, _, _, ids = content_client
    feed = (await client.get("/api/feed")).json()
    history = (await client.get("/api/history")).json()
    answer = await client.post(f"/api/cards/{ids['card_id']}/questions", json={"question": "决定是什么？"})
    refreshed_feed = (await client.get("/api/feed")).json()

    assert feed["todos"][0]["completed"] is False
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


@pytest.mark.asyncio
async def test_todo_due_at_accepts_future_date_and_clears_empty_value(content_client):
    client, _, _, ids = content_client
    future = await client.patch(
        f"/api/todos/{ids['todo_id']}",
        json={"due_at": "2099-08-05T09:00:00+08:00"},
    )
    cleared = await client.patch(f"/api/todos/{ids['todo_id']}", json={"due_at": ""})

    assert future.status_code == 200
    assert future.json()["due_at"] == "2099-08-05T09:00:00+08:00"
    assert future.json()["overdue"] is False
    assert cleared.json()["due_at"] is None
    assert cleared.json()["overdue"] is False


@pytest.mark.asyncio
async def test_todo_due_at_requires_timezone_aware_iso_datetime(content_client):
    client, _, _, ids = content_client
    response = await client.patch(
        f"/api/todos/{ids['todo_id']}", json={"due_at": "2099-08-05T09:00:00"}
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_feed_orders_overdue_incomplete_then_completed_todos(content_client):
    client, _, database, ids = content_client
    async with database.session() as session:
        session.add_all(
            [
                Todo(
                    id=str(uuid4()),
                    batch_id=ids["batch_id"],
                    text="较早的未逾期事项",
                    due_at="2099-08-05T09:00:00+08:00",
                    created_at="2026-08-05T09:00:00+00:00",
                ),
                Todo(
                    id=str(uuid4()),
                    batch_id=ids["batch_id"],
                    text="较新的未逾期事项",
                    due_at="2099-08-05T09:00:00+08:00",
                    created_at="2026-08-05T10:00:00+00:00",
                ),
                Todo(
                    id=str(uuid4()),
                    batch_id=ids["batch_id"],
                    text="已完成事项",
                    completed=True,
                    created_at="2026-08-05T11:00:00+00:00",
                ),
            ]
        )
        await session.commit()

    todos = (await client.get("/api/feed")).json()["todos"]

    assert [todo["text"] for todo in todos] == [
        "已过期事项",
        "较新的未逾期事项",
        "较早的未逾期事项",
        "已完成事项",
    ]


@pytest.mark.asyncio
async def test_user_todo_changes_record_protected_state(content_client):
    client, _, database, ids = content_client

    response = await client.patch(
        f"/api/todos/{ids['todo_id']}",
        json={
            "text": "用户改写",
            "due_at": "2099-08-05T09:00:00+08:00",
            "completed": True,
        },
    )

    assert response.status_code == 200
    async with database.session() as session:
        todo = await session.get(Todo, ids["todo_id"])
    assert todo is not None and todo.user_edited is True
    assert todo.completion_source == "user"


@pytest.mark.asyncio
async def test_deleted_model_todo_creates_non_resurrection_tombstone(content_client):
    client, _, database, ids = content_client

    response = await client.delete(f"/api/todos/{ids['todo_id']}")

    assert response.status_code == 204
    async with database.session() as session:
        assert await session.get(Todo, ids["todo_id"]) is None
        tombstone = await session.get(TodoTombstone, "fingerprint-1")
    assert tombstone is not None


@pytest.mark.asyncio
async def test_feedback_context_uses_card_version_snapshot(content_client):
    _, paths, database, ids = content_client
    context = await ContentService(
        database, paths, FakeQuestionAnswerer()
    ).feedback_context(ids["card_id"])

    assert context["provider_id"] == "kimi"
    assert context["model_id"] == "kimi-k2.5"
    assert context["prompt_snapshot"] == {
        "meeting": {"content": "frozen prompt"}
    }
