from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from audio_memory.analysis.task_coordinator import AnalysisTaskCoordinator
from audio_memory.content.clear import HistoryBusyError, HistoryCleaner
from audio_memory.content.feedback import FeedbackWriter
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    FeedbackIndex,
    ProviderMetadata,
    QAMessage,
    ReanalysisBatch,
    TempFileManifest,
    Todo,
    TodoTombstone,
)


@pytest.mark.asyncio
async def test_feedback_file_contains_scene_audio_transcript_and_complete_qa(tmp_path: Path) -> None:
    database = Database(tmp_path / "feedback.sqlite3")
    await database.create_schema()
    folder = tmp_path / "意见反馈"
    writer = FeedbackWriter(database, folder)

    record = await writer.write(
        card_id="card-1",
        scene_id="parenting",
        rating="inaccurate",
        explanation="原因判断不对",
        audio=[{"name": "家庭录音.mp3", "duration_ms": 1000}],
        transcript="完整原文",
        qa=[{"role": "user", "content": "为什么？"}, {"role": "assistant", "content": "回答"}],
        provider_id="kimi",
        model_id="kimi-k2.5",
        prompt_snapshot={"parenting": {"content": "frozen"}},
    )

    payload = json.loads(Path(record.file_path).read_text())
    assert payload["scene_id"] == "parenting"
    assert payload["audio"][0]["name"] == "家庭录音.mp3"
    assert payload["transcript"] == "完整原文"
    assert len(payload["qa"]) == 2
    assert payload["provider_id"] == "kimi"
    assert payload["model_id"] == "kimi-k2.5"
    assert payload["prompt_snapshot"] == {
        "parenting": {"content": "frozen"}
    }
    await database.dispose()


@pytest.mark.asyncio
async def test_clear_history_preserves_provider_prompts_and_feedback(tmp_path: Path) -> None:
    database = Database(tmp_path / "clear.sqlite3")
    await database.create_schema()
    prompts = tmp_path / "prompts"
    feedback = tmp_path / "意见反馈"
    audio = tmp_path / "audio"
    staging = tmp_path / "staging"
    for folder in (prompts, feedback, audio, staging):
        folder.mkdir()
    (prompts / "keep.md").write_text("prompt")
    (feedback / "keep.json").write_text("{}")
    (audio / "delete.mp3").write_bytes(b"audio")
    (staging / "delete-staged.mp3").write_bytes(b"staged audio")
    async with database.session() as session:
        session.add(ProviderMetadata(provider_id="kimi", validation_status="available"))
        session.add(FeedbackIndex(id="feedback-1", scene_id="meeting", file_path=str(feedback / "keep.json"), rating="accurate"))
        session.add(AnalysisJob(id="job-1", stage="completed"))
        session.add(AnalysisJob(id="job-pending", stage="analyzing"))
        session.add(
            TempFileManifest(
                id="manifest-pending",
                task_uuid="job-pending",
                file_path=str(staging / "delete-staged.mp3"),
            )
        )
        session.add(Batch(id="batch-1", job_id="job-1", natural_date="2026-08-01"))
        await session.flush()
        session.add(
            AnalysisVersion(
                id="version-1",
                source_job_id="job-1",
                batch_id="batch-1",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="completed",
            )
        )
        session.add(
            AnalysisVersion(
                id="version-pending",
                source_job_id="job-pending",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="failed",
            )
        )
        session.add(
            ReanalysisBatch(
                id="history-orphan",
                status="stopped",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                snapshot_hash="snapshot",
            )
        )
        await session.flush()
        batch = await session.get(Batch, "batch-1")
        assert batch is not None
        batch.current_analysis_version_id = "version-1"
        session.add(
            Card(
                id="card-1",
                batch_id="batch-1",
                analysis_version_id="version-1",
                scene_id="meeting",
                position=0,
                payload_json="{}",
            )
        )
        await session.flush()
        session.add_all(
            [
                QAMessage(
                    id="qa-1",
                    card_id="card-1",
                    role="user",
                    content="question",
                    position=0,
                ),
                Todo(
                    id="todo-1",
                    batch_id="batch-1",
                    analysis_version_id="version-1",
                    source_job_id="job-1",
                    source_fingerprint="source-1",
                    text="todo",
                ),
                TodoTombstone(source_fingerprint="deleted-source"),
            ]
        )
        await session.commit()

    await HistoryCleaner(database, audio, staging).clear(confirm=True)

    assert list(audio.iterdir()) == []
    assert list(staging.iterdir()) == []
    assert (prompts / "keep.md").exists()
    assert (feedback / "keep.json").exists()
    async with database.session() as session:
        assert await session.get(ProviderMetadata, "kimi") is not None
        assert await session.get(FeedbackIndex, "feedback-1") is not None
        assert int(await session.scalar(select(func.count(AnalysisJob.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(AnalysisVersion.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(Card.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(QAMessage.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(Todo.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(TodoTombstone.source_fingerprint))) or 0) == 0
        assert int(await session.scalar(select(func.count(ReanalysisBatch.id))) or 0) == 0
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running"])
async def test_clear_history_rejects_ordinary_analysis_work(
    tmp_path: Path, status: str
) -> None:
    database = Database(tmp_path / f"ordinary-{status}.sqlite3")
    await database.create_schema()
    audio = tmp_path / f"audio-{status}"
    async with database.session() as session:
        session.add(AnalysisJob(id="ordinary-job", stage="analyzing"))
        session.add(
            AnalysisVersion(
                id="ordinary-version",
                source_job_id="ordinary-job",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status=status,
            )
        )
        await session.commit()

    cleaner = HistoryCleaner(
        database,
        audio,
        task_coordinator=AnalysisTaskCoordinator(database),
    )
    with pytest.raises(HistoryBusyError, match="analysis work"):
        await cleaner.clear(confirm=True)

    async with database.session() as session:
        assert await session.get(AnalysisJob, "ordinary-job") is not None
    await database.dispose()


@pytest.mark.asyncio
async def test_clear_history_racing_profile_retry_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "profile-race.sqlite3")
    await database.create_schema()
    coordinator = AnalysisTaskCoordinator(database)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def profile_retry() -> None:
        async with coordinator.profile_retry_guard():
            entered.set()
            await release.wait()

    retry_task = asyncio.create_task(profile_retry())
    await entered.wait()
    cleaner = HistoryCleaner(
        database,
        tmp_path / "profile-audio",
        task_coordinator=coordinator,
    )
    clear_task = asyncio.create_task(cleaner.clear(confirm=True))
    await asyncio.sleep(0)
    assert not clear_task.done()

    release.set()
    await retry_task
    with pytest.raises(HistoryBusyError, match="profile rebuild"):
        await clear_task
    await database.dispose()
