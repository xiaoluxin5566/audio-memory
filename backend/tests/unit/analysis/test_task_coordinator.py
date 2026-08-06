from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from audio_memory.analysis.task_coordinator import (
    AlreadyRunningError,
    AnalysisRequest,
    AnalysisTaskCoordinator,
)
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    ReanalysisBatch,
    ReanalysisItem,
)


def request(job_id: str, *, batch_id: str | None, priority: int) -> AnalysisRequest:
    return AnalysisRequest(
        source_job_id=job_id,
        source_batch_id=batch_id,
        provider_id="kimi",
        model_id="kimi-k2.5",
        credential_generation=3,
        prompt_snapshot={"meeting": {"version": 2, "content": "meeting"}},
        profile_snapshot=[{"subject_id": "user", "dimension": "role"}],
        priority=priority,
    )


async def seed_jobs(database: Database, *job_ids: str) -> None:
    async with database.session() as session:
        session.add_all(AnalysisJob(id=job_id, stage="analyzing") for job_id in job_ids)
        await session.commit()


@pytest.mark.asyncio
async def test_new_upload_priority_precedes_history(tmp_path) -> None:
    database = Database(tmp_path / "priority.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-old", "job-new")
    coordinator = AnalysisTaskCoordinator(database)
    old = request("job-old", batch_id=None, priority=10)
    new = request("job-new", batch_id=None, priority=0)

    await coordinator.submit_reanalysis(old)
    await coordinator.submit_new_upload(new)

    assert await coordinator.next_request() == new
    await database.dispose()


@pytest.mark.asyncio
async def test_same_source_cannot_be_pending_or_running_twice(tmp_path) -> None:
    database = Database(tmp_path / "exclusive.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-same")
    coordinator = AnalysisTaskCoordinator(database)
    item = request("job-same", batch_id=None, priority=10)

    await coordinator.submit_reanalysis(item)
    with pytest.raises(AlreadyRunningError):
        await coordinator.submit_reanalysis(item)
    assert await coordinator.next_request() == item
    with pytest.raises(AlreadyRunningError):
        await coordinator.submit_reanalysis(item)
    await database.dispose()


@pytest.mark.asyncio
async def test_restart_returns_running_request_to_pending(tmp_path) -> None:
    database = Database(tmp_path / "restart.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-restart")
    first = AnalysisTaskCoordinator(database)
    item = request("job-restart", batch_id=None, priority=0)
    await first.submit_new_upload(item)
    assert await first.next_request() == item

    restarted = AnalysisTaskCoordinator(database)
    assert await restarted.next_request() == item
    async with database.session() as session:
        version = await session.scalar(
            select(AnalysisVersion).where(
                AnalysisVersion.source_job_id == "job-restart"
            )
        )
    assert version is not None and version.status == "running"
    await database.dispose()


@pytest.mark.asyncio
async def test_stopped_history_batch_does_not_yield_a_new_item(tmp_path) -> None:
    database = Database(tmp_path / "stopped.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-history")
    async with database.session() as session:
        session.add(Batch(id="batch-source", job_id="job-history", natural_date="2026-08-05"))
        session.add(
            ReanalysisBatch(
                id="history-run",
                status="stopped",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=3,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="f" * 64,
                snapshot_hash="s" * 64,
            )
        )
        session.add(
            ReanalysisItem(
                id="history-item",
                reanalysis_batch_id="history-run",
                source_batch_id="batch-source",
                position=0,
                status="pending",
            )
        )
        await session.commit()
    coordinator = AnalysisTaskCoordinator(database)
    await coordinator.submit_reanalysis(
        request("job-history", batch_id="batch-source", priority=10)
    )

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(coordinator.next_request(), timeout=0.05)
    async with database.session() as session:
        version = await session.scalar(select(AnalysisVersion))
    assert version is not None and version.status == "pending"
    await database.dispose()


@pytest.mark.asyncio
async def test_new_upload_submission_marks_failed_job_analyzing(tmp_path) -> None:
    database = Database(tmp_path / "retry-stage.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(
            AnalysisJob(
                id="job-retry",
                stage="failed",
                error_code="model_analysis_failed",
            )
        )
        await session.commit()
    coordinator = AnalysisTaskCoordinator(database)

    await coordinator.submit_new_upload(
        request("job-retry", batch_id=None, priority=0)
    )

    async with database.session() as session:
        job = await session.get(AnalysisJob, "job-retry")
    assert job is not None
    assert job.stage == "analyzing"
    assert job.error_code is None
    await database.dispose()


@pytest.mark.asyncio
async def test_fixed_rules_hash_is_independent_of_user_prompt_snapshot(tmp_path) -> None:
    database = Database(tmp_path / "fixed-rules.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-prompt-a", "job-prompt-b")
    coordinator = AnalysisTaskCoordinator(database)
    first = request("job-prompt-a", batch_id=None, priority=0)
    second = AnalysisRequest(
        source_job_id="job-prompt-b",
        source_batch_id=None,
        provider_id=first.provider_id,
        model_id=first.model_id,
        credential_generation=first.credential_generation,
        prompt_snapshot={"meeting": {"version": 99, "content": "user edit"}},
        profile_snapshot=first.profile_snapshot,
        priority=0,
    )

    await coordinator.submit_new_upload(first)
    await coordinator.submit_new_upload(second)

    async with database.session() as session:
        hashes = list(
            await session.scalars(
                select(AnalysisVersion.fixed_rules_hash).order_by(
                    AnalysisVersion.source_job_id
                )
            )
        )
    assert len(hashes) == 2
    assert hashes[0] == hashes[1]
    assert len(hashes[0]) == 64
    await database.dispose()
