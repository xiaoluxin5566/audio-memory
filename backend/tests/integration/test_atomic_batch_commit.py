from pathlib import Path

import pytest

from audio_memory.db import Database
from audio_memory.models import AnalysisJob
from audio_memory.repositories import BatchRepository


@pytest.fixture
def meeting_card() -> dict[str, object]:
    return {
        "id": "card-meeting",
        "scene_id": "meeting",
        "position": 0,
        "payload": {"title": "产品评审", "summary": "确认第一阶段范围"},
    }


@pytest.mark.asyncio
async def test_create_job_rejects_unknown_stage(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-stage.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        with pytest.raises(ValueError, match="Unsupported job stage"):
            await repository.create_job(stage="nonsense")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_uncommitted_batch_is_absent_from_feed(
    tmp_path: Path,
    meeting_card: dict[str, object],
) -> None:
    database = Database(tmp_path / "draft.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        job = await repository.create_job(stage="analyzing")
        await repository.stage_card(job.id, meeting_card)

        assert await repository.list_feed_batches() == []
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_publish_batch_exposes_all_staged_cards_atomically(
    tmp_path: Path,
    meeting_card: dict[str, object],
) -> None:
    database = Database(tmp_path / "publish.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        job = await repository.create_job(stage="ready_to_commit")
        await repository.stage_card(job.id, meeting_card)

        batch = await repository.publish_batch(job.id, batch_id="batch-1")

        assert batch.id == "batch-1"
        assert batch.card_count == 1
        feed = await repository.list_feed_batches()
        assert [(item.id, item.card_count) for item in feed] == [("batch-1", 1)]
        persisted_job = await repository.get_job(job.id)
        assert persisted_job.stage == "completed"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_publish_failure_rolls_back_batch_cards_and_job_state(
    tmp_path: Path,
    meeting_card: dict[str, object],
) -> None:
    database = Database(tmp_path / "rollback.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        job = await repository.create_job(stage="ready_to_commit")
        await repository.stage_card(job.id, meeting_card)
        await repository.stage_card(job.id, meeting_card)

        with pytest.raises(Exception):
            await repository.publish_batch(job.id, batch_id="batch-rollback")

        assert await repository.list_feed_batches() == []
        persisted_job: AnalysisJob = await repository.get_job(job.id)
        assert persisted_job.stage == "ready_to_commit"
    finally:
        await database.dispose()
