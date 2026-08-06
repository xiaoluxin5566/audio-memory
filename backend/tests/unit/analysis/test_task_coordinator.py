from __future__ import annotations

import asyncio
from dataclasses import replace
import json

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
from audio_memory.prompts.composer import PromptComposer


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


async def seed_active_history(
    database: Database,
    *,
    job_id: str,
    batch_id: str,
    run_id: str,
) -> None:
    async with database.session() as session:
        session.add(Batch(id=batch_id, job_id=job_id, natural_date="2026-08-05"))
        session.add(
            ReanalysisBatch(
                id=run_id,
                status="running",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=3,
                prompt_snapshot_json=json.dumps(
                    {"meeting": {"version": 2, "content": "meeting"}}
                ),
                profile_snapshot_json=json.dumps(
                    [{"subject_id": "user", "dimension": "role"}]
                ),
                fixed_rules_hash=PromptComposer.fixed_rules_hash(),
                snapshot_hash="s" * 64,
            )
        )
        session.add(
            ReanalysisItem(
                id=f"{run_id}-item",
                reanalysis_batch_id=run_id,
                source_batch_id=batch_id,
                position=0,
                status="pending",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_new_upload_priority_precedes_history(tmp_path) -> None:
    database = Database(tmp_path / "priority.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-old", "job-new")
    await seed_active_history(
        database,
        job_id="job-old",
        batch_id="batch-old",
        run_id="run-old",
    )
    coordinator = AnalysisTaskCoordinator(database)
    old = request("job-old", batch_id="batch-old", priority=10)
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
    item = request("job-same", batch_id=None, priority=0)

    await coordinator.submit_new_upload(item)
    with pytest.raises(AlreadyRunningError):
        await coordinator.submit_new_upload(item)
    assert await coordinator.next_request() == item
    with pytest.raises(AlreadyRunningError):
        await coordinator.submit_new_upload(item)
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
    async with database.session() as session:
        version = await session.scalar(select(AnalysisVersion))
        assert version is not None
        version.lease_expires_at = "2000-01-01T00:00:00+00:00"
        await session.commit()

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
async def test_restart_returns_linked_history_item_to_pending(tmp_path) -> None:
    database = Database(tmp_path / "history-restart.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-history-restart")
    await seed_active_history(
        database,
        job_id="job-history-restart",
        batch_id="batch-history-restart",
        run_id="run-history-restart",
    )
    first = AnalysisTaskCoordinator(database)
    item = request(
        "job-history-restart", batch_id="batch-history-restart", priority=10
    )
    await first.submit_reanalysis(item)
    assert await first.next_request() == item
    async with database.session() as session:
        version = await session.scalar(select(AnalysisVersion))
        assert version is not None
        version.lease_expires_at = "2000-01-01T00:00:00+00:00"
        await session.commit()

    await AnalysisTaskCoordinator(database).initialize()

    async with database.session() as session:
        stored_item = await session.get(
            ReanalysisItem, "run-history-restart-item"
        )
        version = await session.scalar(select(AnalysisVersion))
    assert version is not None and version.status == "pending"
    assert stored_item is not None and stored_item.status == "pending"
    await database.dispose()


@pytest.mark.asyncio
async def test_close_releases_linked_history_item_with_owned_claim(tmp_path) -> None:
    database = Database(tmp_path / "history-close.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-history-close")
    await seed_active_history(
        database,
        job_id="job-history-close",
        batch_id="batch-history-close",
        run_id="run-history-close",
    )
    coordinator = AnalysisTaskCoordinator(database)
    item = request("job-history-close", batch_id="batch-history-close", priority=10)
    await coordinator.submit_reanalysis(item)
    assert await coordinator.next_request() == item

    await coordinator.close()

    async with database.session() as session:
        stored_item = await session.get(ReanalysisItem, "run-history-close-item")
        version = await session.scalar(select(AnalysisVersion))
    assert version is not None and version.status == "pending"
    assert stored_item is not None and stored_item.status == "pending"
    await database.dispose()


@pytest.mark.parametrize(
    "paused_status",
    ("stopped", "paused_rules_changed", "paused_error"),
)
@pytest.mark.asyncio
async def test_paused_history_batch_does_not_yield_a_new_item(
    tmp_path, paused_status: str
) -> None:
    database = Database(tmp_path / "stopped.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-history")
    async with database.session() as session:
        session.add(Batch(id="batch-source", job_id="job-history", natural_date="2026-08-05"))
        session.add(
            ReanalysisBatch(
                id="history-run",
                status="running",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=3,
                prompt_snapshot_json=json.dumps(
                    {"meeting": {"version": 2, "content": "meeting"}}
                ),
                profile_snapshot_json=json.dumps(
                    [{"subject_id": "user", "dimension": "role"}]
                ),
                fixed_rules_hash=PromptComposer.fixed_rules_hash(),
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
    async with database.session() as session:
        history_run = await session.get(ReanalysisBatch, "history-run")
        assert history_run is not None
        history_run.status = paused_status
        await session.commit()

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


@pytest.mark.asyncio
async def test_two_coordinators_cannot_claim_the_same_pending_version(tmp_path) -> None:
    database = Database(tmp_path / "atomic-claim.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-atomic")
    first = AnalysisTaskCoordinator(database)
    second = AnalysisTaskCoordinator(database)
    await first.initialize()
    await second.initialize()
    item = request("job-atomic", batch_id=None, priority=0)
    await first.submit_new_upload(item)

    first_claim = asyncio.create_task(first.next_request())
    second_claim = asyncio.create_task(second.next_request())
    done, pending = await asyncio.wait(
        {first_claim, second_claim}, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
    )
    await asyncio.sleep(0.05)

    assert len(done) == 1
    assert next(iter(done)).result() == item
    assert sum(task.done() for task in (first_claim, second_claim)) == 1
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await database.dispose()


@pytest.mark.asyncio
async def test_reanalysis_requires_a_source_batch(tmp_path) -> None:
    database = Database(tmp_path / "history-source-required.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-history-required")
    coordinator = AnalysisTaskCoordinator(database)

    with pytest.raises(ValueError, match="source_batch_id"):
        await coordinator.submit_reanalysis(
            request("job-history-required", batch_id=None, priority=10)
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_reanalysis_requires_active_owning_item_and_matching_job(tmp_path) -> None:
    database = Database(tmp_path / "history-owner.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-request", "job-batch")
    async with database.session() as session:
        session.add(
            Batch(
                id="batch-other-job",
                job_id="job-batch",
                natural_date="2026-08-06",
            )
        )
        await session.commit()
    coordinator = AnalysisTaskCoordinator(database)

    with pytest.raises(ValueError, match="active owning"):
        await coordinator.submit_reanalysis(
            request("job-request", batch_id="batch-other-job", priority=10)
        )

    async with database.session() as session:
        session.add(
            ReanalysisBatch(
                id="history-owner",
                status="running",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=3,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="f" * 64,
                snapshot_hash="s" * 64,
            )
        )
        await session.flush()
        session.add(
            ReanalysisItem(
                id="history-owner-item",
                reanalysis_batch_id="history-owner",
                source_batch_id="batch-other-job",
                position=0,
                status="pending",
            )
        )
        await session.commit()

    with pytest.raises(ValueError, match="source job"):
        await coordinator.submit_reanalysis(
            request("job-request", batch_id="batch-other-job", priority=10)
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_reanalysis_must_match_the_owning_run_snapshot(tmp_path) -> None:
    database = Database(tmp_path / "history-snapshot.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-history-snapshot")
    await seed_active_history(
        database,
        job_id="job-history-snapshot",
        batch_id="batch-history-snapshot",
        run_id="run-history-snapshot",
    )
    coordinator = AnalysisTaskCoordinator(database)
    altered = replace(
        request(
            "job-history-snapshot",
            batch_id="batch-history-snapshot",
            priority=10,
        ),
        prompt_snapshot={"meeting": {"version": 3, "content": "altered"}},
    )

    with pytest.raises(ValueError, match="snapshot"):
        await coordinator.submit_reanalysis(altered)
    await database.dispose()


@pytest.mark.asyncio
async def test_initializing_second_coordinator_does_not_steal_live_claim(tmp_path) -> None:
    database = Database(tmp_path / "live-owner.sqlite3")
    await database.create_schema()
    await seed_jobs(database, "job-live-owner")
    first = AnalysisTaskCoordinator(database)
    item = request("job-live-owner", batch_id=None, priority=0)
    await first.submit_new_upload(item)
    assert await first.next_request() == item

    second = AnalysisTaskCoordinator(database)
    await second.initialize()

    async with database.session() as session:
        version = await session.scalar(select(AnalysisVersion))
    assert version is not None
    assert version.status == "running"
    assert version.worker_owner_id == first.owner_id
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(second.next_request(), timeout=0.05)
    await database.dispose()
