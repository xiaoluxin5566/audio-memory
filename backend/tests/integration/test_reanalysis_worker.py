from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import func, select

from audio_memory.analysis.task_coordinator import AnalysisTaskCoordinator
from audio_memory.content.clear import HistoryBusyError, HistoryCleaner
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    JobFile,
    ReanalysisBatch,
    ReanalysisItem,
    Transcript,
)
from audio_memory.prompts.composer import PromptComposer
from audio_memory.providers.types import ProviderState, ProviderStateName


PROMPTS = {"meeting": {"version": 2, "content": "frozen meeting prompt"}}


class Provider:
    def __init__(self, generation: int = 3) -> None:
        self.generation = generation

    async def validate_saved(self, provider_id: str):
        return type("Validation", (), {"ok": provider_id == "kimi"})()

    async def snapshot_active_with_generation(self):
        return (
            ProviderState(
                provider_id="kimi",
                display_name="Kimi",
                model_id="kimi-k2.5",
                active=True,
                state=ProviderStateName.AVAILABLE,
            ),
            self.generation,
        )


class ProfilePublisher:
    def __init__(self, database: Database, *, fail: bool = False) -> None:
        self.database = database
        self.fail = fail
        self.calls: list[str] = []

    async def retry_profile(self, batch_id: str) -> None:
        self.calls.append(batch_id)
        if self.fail:
            raise RuntimeError("profile rebuild failed")
        async with self.database.session() as session:
            batch = await session.get(ReanalysisBatch, batch_id)
            assert batch is not None
            failed = int(
                await session.scalar(
                    select(func.count(ReanalysisItem.id)).where(
                        ReanalysisItem.reanalysis_batch_id == batch_id,
                        ReanalysisItem.status == "failed",
                    )
                )
                or 0
            )
            batch.status = "completed_with_failures" if failed else "completed"
            await session.commit()


class BlockingProfilePublisher(ProfilePublisher):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def retry_profile(self, batch_id: str) -> None:
        self.entered.set()
        await self.release.wait()
        await super().retry_profile(batch_id)


def valid_event_map(segment_id: str) -> str:
    return json.dumps(
        {
            "user_speaker": {
                "speaker_id": "speaker_0",
                "confidence": 0.9,
                "reasoning": "explicit first-person evidence",
                "evidence_segment_ids": [segment_id],
            },
            "events": [
                {
                    "event_id": "event_1",
                    "parent_event_id": None,
                    "title": "Meeting",
                    "event_type": "meeting",
                    "local_date": "2026-08-05",
                    "timezone": None,
                    "start_ms": 0,
                    "end_ms": 1000,
                    "speaker_ids": ["speaker_0"],
                    "user_role": "participant",
                    "user_role_confidence": 0.9,
                    "factual_summary": "Summary",
                    "topics": ["meeting"],
                    "candidate_scenes": ["meeting"],
                    "evidence_segment_ids": [segment_id],
                    "boundary_confidence": 0.9,
                }
            ],
            "unassigned_segment_ids": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def seed_history_run(
    database: Database,
    *,
    batch_status: str = "running",
    item_statuses: tuple[str, ...] = ("pending", "pending"),
    fixed_rules_hash: str | None = None,
) -> None:
    fixed_hash = fixed_rules_hash or PromptComposer.fixed_rules_hash()
    async with database.session() as session:
        for position in range(len(item_statuses)):
            job_id = f"job-{position}"
            source_id = f"source-{position}"
            session.add(AnalysisJob(id=job_id, stage="completed"))
            session.add(
                Batch(
                    id=source_id,
                    job_id=job_id,
                    natural_date="2026-08-05",
                    uploaded_at=f"2026-08-0{position + 1}T12:00:00+00:00",
                )
            )
            session.add(
                JobFile(
                    id=f"file-{position}",
                    job_id=job_id,
                    original_name=f"audio-{position}.mp3",
                    extension=".mp3",
                    size_bytes=8,
                    sha256=str(position) * 64,
                    position=0,
                    temporary_path=f"/audio/audio-{position}.mp3",
                )
            )
            session.add(
                Transcript(
                    id=f"transcript-{position}",
                    job_file_id=f"file-{position}",
                    segment_index=0,
                    segment_uid=f"file-{position}:0",
                    speaker_id="speaker_0",
                    start_ms=0,
                    end_ms=1000,
                    text=f"text-{position}",
                    words_json="[]",
                    risk_classified=True,
                )
            )
        await session.flush()
        for position in range(len(item_statuses)):
            event_json = valid_event_map("seg_0_0")
            session.add(
                AnalysisVersion(
                    id=f"old-version-{position}",
                    source_job_id=f"job-{position}",
                    batch_id=f"source-{position}",
                    provider_id="kimi",
                    model_id="old-model",
                    credential_generation=1,
                    prompt_snapshot_json="{}",
                    profile_snapshot_json="[]",
                    fixed_rules_hash=fixed_hash,
                    event_map_json=event_json,
                    event_map_hash=sha256(event_json.encode()).hexdigest(),
                    staged_results_json="{}",
                    priority=0,
                    status="completed",
                )
            )
        await session.flush()
        for position in range(len(item_statuses)):
            source = await session.get(Batch, f"source-{position}")
            assert source is not None
            source.current_analysis_version_id = f"old-version-{position}"
        session.add(
            ReanalysisBatch(
                id="history-1",
                status=batch_status,
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=3,
                prompt_snapshot_json=json.dumps(PROMPTS),
                profile_snapshot_json='[{"subject_id":"user","dimension":"role"}]',
                fixed_rules_hash=fixed_hash,
                snapshot_hash="s" * 64,
            )
        )
        await session.flush()
        for position, item_status in enumerate(item_statuses):
            # Position zero is the newest source.
            source_position = len(item_statuses) - position - 1
            session.add(
                ReanalysisItem(
                    id=f"item-{position}",
                    reanalysis_batch_id="history-1",
                    source_batch_id=f"source-{source_position}",
                    position=position,
                    status=item_status,
                )
            )
        await session.commit()
    from audio_memory.reanalysis.preview import (
        canonical_hash,
        current_fixed_rule_hashes,
        transcript_fingerprint,
    )

    fingerprints = {
        f"source-{position}": await transcript_fingerprint(
            database, f"job-{position}"
        )
        for position in range(len(item_statuses))
    }
    metadata = {
        "fixed_rule_hashes": current_fixed_rule_hashes(),
        "transcript_fingerprints": fingerprints,
        "profile_hash": canonical_hash(
            [{"subject_id": "user", "dimension": "role"}]
        ),
    }
    async with database.session() as session:
        history = await session.get(ReanalysisBatch, "history-1")
        assert history is not None
        history.prompt_snapshot_json = json.dumps(
            {**PROMPTS, "_reanalysis": metadata}, sort_keys=True
        )
        for position in range(len(item_statuses)):
            version = await session.get(AnalysisVersion, f"old-version-{position}")
            assert version is not None
            version.prompt_snapshot_json = json.dumps(
                {"_reanalysis": metadata}, sort_keys=True
            )
            version.profile_snapshot_json = history.profile_snapshot_json
        await session.commit()


@pytest.mark.asyncio
async def test_worker_enqueues_one_newest_item_reuses_valid_event_map_and_never_writes_audio_or_transcript(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "worker.sqlite3")
    await database.create_schema()
    await seed_history_run(database)
    coordinator = AnalysisTaskCoordinator(database)
    publisher = ProfilePublisher(database)
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=coordinator,
        publisher=publisher,
        poll_interval=0.01,
    )
    async with database.session() as session:
        before_files = int(await session.scalar(select(func.count(JobFile.id))) or 0)
        before_transcripts = int(
            await session.scalar(select(func.count(Transcript.id))) or 0
        )

    await worker.tick()
    await worker.tick()

    async with database.session() as session:
        versions = list(
            await session.scalars(
                select(AnalysisVersion).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
        )
        items = list(
            await session.scalars(
                select(ReanalysisItem).order_by(ReanalysisItem.position)
            )
        )
        after_files = int(await session.scalar(select(func.count(JobFile.id))) or 0)
        after_transcripts = int(
            await session.scalar(select(func.count(Transcript.id))) or 0
        )
    assert len(versions) == 1
    assert versions[0].source_job_id == "job-1"
    assert versions[0].priority == 10
    assert versions[0].event_map_json == valid_event_map("seg_0_0")
    assert [item.status for item in items] == ["pending", "pending"]
    assert items[0].analysis_version_id == versions[0].id
    assert items[1].analysis_version_id is None
    assert (after_files, after_transcripts) == (before_files, before_transcripts)
    await database.dispose()


@pytest.mark.asyncio
async def test_event_map_is_regenerated_when_frozen_profile_changed(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import canonical_hash
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "profile-event-map.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("pending",))
    async with database.session() as session:
        source_version = await session.get(AnalysisVersion, "old-version-0")
        assert source_version is not None
        source_version.profile_snapshot_json = "[]"
        source_prompts = json.loads(source_version.prompt_snapshot_json)
        source_prompts["_reanalysis"]["profile_hash"] = canonical_hash([])
        source_version.prompt_snapshot_json = json.dumps(source_prompts, sort_keys=True)
        await session.commit()
    coordinator = AnalysisTaskCoordinator(database)
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=coordinator,
        publisher=ProfilePublisher(database),
    )

    await worker.tick()

    async with database.session() as session:
        generated = await session.scalar(
            select(AnalysisVersion).where(
                AnalysisVersion.reanalysis_batch_id == "history-1"
            )
        )
        batch = await session.get(ReanalysisBatch, "history-1")
    assert generated is not None
    assert generated.event_map_json is None
    assert generated.event_map_hash is None
    assert batch is not None and batch.status == "running"
    await database.dispose()


@pytest.mark.asyncio
async def test_event_map_is_not_reused_when_profile_metadata_disagrees_with_version(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "profile-metadata-defense.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("pending",))
    async with database.session() as session:
        source_version = await session.get(AnalysisVersion, "old-version-0")
        assert source_version is not None
        source_version.profile_snapshot_json = "[]"
        await session.commit()
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=AnalysisTaskCoordinator(database),
        publisher=ProfilePublisher(database),
    )

    await worker.tick()

    async with database.session() as session:
        generated = await session.scalar(
            select(AnalysisVersion).where(
                AnalysisVersion.reanalysis_batch_id == "history-1"
            )
        )
    assert generated is not None
    assert generated.event_map_json is None
    assert generated.event_map_hash is None
    await database.dispose()


@pytest.mark.asyncio
async def test_stop_fences_pending_remote_work_and_waits_for_running_item(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "stop.sqlite3")
    await database.create_schema()
    await seed_history_run(database)
    coordinator = AnalysisTaskCoordinator(database)
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=coordinator,
        publisher=ProfilePublisher(database),
    )
    await worker.tick()
    _request = await coordinator.next_request()
    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        assert batch is not None
        batch.status = "stopping"
        await session.commit()

    await worker.tick()
    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        pending = await session.get(ReanalysisItem, "item-1")
    assert batch is not None and batch.status == "stopping"
    assert pending is not None and pending.status == "pending"

    async with database.session() as session:
        version = await session.scalar(
            select(AnalysisVersion).where(
                AnalysisVersion.reanalysis_batch_id == "history-1"
            )
        )
        current = await session.get(ReanalysisItem, "item-0")
        assert version is not None and current is not None
        version.status = "completed"
        version.worker_owner_id = None
        version.lease_expires_at = None
        current.status = "succeeded"
        await session.commit()
    await worker.tick()

    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        items = list(
            await session.scalars(
                select(ReanalysisItem).order_by(ReanalysisItem.position)
            )
        )
        versions = list(
            await session.scalars(
                select(AnalysisVersion).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
        )
    assert batch is not None and batch.status == "stopped"
    assert [item.status for item in items] == ["succeeded", "stopped"]
    assert len(versions) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_restart_recovers_running_item_with_same_version_and_snapshot(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "restart.sqlite3")
    await database.create_schema()
    await seed_history_run(database)
    coordinator = AnalysisTaskCoordinator(database)
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=coordinator,
        publisher=ProfilePublisher(database),
    )
    await worker.tick()
    await coordinator.next_request()
    async with database.session() as session:
        version = await session.scalar(
            select(AnalysisVersion).where(
                AnalysisVersion.reanalysis_batch_id == "history-1"
            )
        )
        assert version is not None
        original_id = version.id
        original_prompt = version.prompt_snapshot_json

    await worker.recover()
    await worker.tick()

    async with database.session() as session:
        versions = list(
            await session.scalars(
                select(AnalysisVersion).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
        )
        item = await session.get(ReanalysisItem, "item-0")
    assert len(versions) == 1
    assert versions[0].id == original_id
    assert versions[0].status == "pending"
    assert versions[0].prompt_snapshot_json == original_prompt
    assert item is not None and item.status == "pending"
    await database.dispose()


@pytest.mark.asyncio
async def test_restart_repairs_stale_running_items_without_live_work(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "stale-items.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("running", "running"))
    async with database.session() as session:
        failed = AnalysisVersion(
            id="failed-version",
            source_job_id="job-0",
            batch_id="source-0",
            provider_id="kimi",
            model_id="kimi-k2.5",
            credential_generation=3,
            prompt_snapshot_json=json.dumps(PROMPTS),
            profile_snapshot_json="[]",
            fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            staged_results_json="{}",
            priority=10,
            status="failed",
            error_code="model_response_invalid",
            reanalysis_batch_id="history-1",
        )
        session.add(failed)
        item = await session.get(ReanalysisItem, "item-1")
        assert item is not None
        item.analysis_version_id = failed.id
        await session.commit()
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=AnalysisTaskCoordinator(database),
        publisher=ProfilePublisher(database),
    )

    await worker.recover()

    async with database.session() as session:
        missing_link = await session.get(ReanalysisItem, "item-0")
        failed_link = await session.get(ReanalysisItem, "item-1")
    assert missing_link is not None and missing_link.status == "pending"
    assert failed_link is not None and failed_link.status == "failed"
    assert failed_link.error_code == "model_response_invalid"
    await database.dispose()


@pytest.mark.asyncio
async def test_ordinary_failure_continues_and_final_profile_rebuild_runs_once(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "partial.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("failed", "pending"))
    coordinator = AnalysisTaskCoordinator(database)
    publisher = ProfilePublisher(database)
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=coordinator,
        publisher=publisher,
    )

    await worker.tick()
    async with database.session() as session:
        second = await session.get(ReanalysisItem, "item-1")
        assert second is not None and second.analysis_version_id is not None
        version = await session.get(AnalysisVersion, second.analysis_version_id)
        assert version is not None
        version.status = "failed"
        second.status = "failed"
        await session.commit()
    await worker.tick()

    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
    assert publisher.calls == ["history-1"]
    assert batch is not None and batch.status == "completed_with_failures"
    await database.dispose()


@pytest.mark.asyncio
async def test_old_attempted_profile_failure_does_not_starve_newer_batch(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "profile-starvation.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("pending",))
    async with database.session() as session:
        current = await session.get(ReanalysisBatch, "history-1")
        assert current is not None
        current.created_at = "2026-08-06T01:00:00+00:00"
        session.add(
            ReanalysisBatch(
                id="history-old-profile-failure",
                status="content_completed_profile_failed",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=3,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash=PromptComposer.fixed_rules_hash(),
                snapshot_hash="o" * 64,
                created_at="2026-08-05T01:00:00+00:00",
                completed_at="2026-08-05T02:00:00+00:00",
            )
        )
        await session.commit()
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=AnalysisTaskCoordinator(database),
        publisher=ProfilePublisher(database),
    )

    await worker.tick()

    async with database.session() as session:
        generated = int(
            await session.scalar(
                select(func.count(AnalysisVersion.id)).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
            or 0
        )
        old = await session.get(ReanalysisBatch, "history-old-profile-failure")
    assert generated == 1
    assert old is not None
    assert old.status == "content_completed_profile_failed"
    await database.dispose()


@pytest.mark.asyncio
async def test_profile_retry_does_not_submit_scene_or_model_work(tmp_path: Path) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService

    database = Database(tmp_path / "profile-only.sqlite3")
    await database.create_schema()
    await seed_history_run(
        database,
        batch_status="content_completed_profile_failed",
        item_statuses=("succeeded",),
    )
    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        assert batch is not None
        batch.completed_at = "2026-08-06T00:00:00+00:00"
        await session.commit()
    publisher = ProfilePublisher(database)
    coordinator = AnalysisTaskCoordinator(database)
    provider = Provider()
    service = ReanalysisService(
        database=database,
        preview_builder=ReanalysisPreviewBuilder(
            database=database,
            prompt_store=__import__(
                "audio_memory.prompts.store", fromlist=["PromptStore"]
            ).PromptStore(tmp_path / "profile-prompts"),
            provider_coordinator=provider,
            signer=PreviewSigner(secret=b"p" * 32),
        ),
        provider_coordinator=provider,
        publisher=publisher,
    )

    view = await service.retry_profile("history-1")

    async with database.session() as session:
        generated = int(
            await session.scalar(
                select(func.count(AnalysisVersion.id)).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
            or 0
        )
    assert publisher.calls == ["history-1"]
    assert view.status == "completed"
    assert generated == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_profile_retry_and_clear_history_share_the_global_fence(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService

    database = Database(tmp_path / "profile-clear-race.sqlite3")
    await database.create_schema()
    await seed_history_run(
        database,
        batch_status="content_completed_profile_failed",
        item_statuses=("succeeded",),
    )
    coordinator = AnalysisTaskCoordinator(database)
    publisher = BlockingProfilePublisher(database)
    service = ReanalysisService(
        database=database,
        preview_builder=ReanalysisPreviewBuilder(
            database=database,
            prompt_store=__import__(
                "audio_memory.prompts.store", fromlist=["PromptStore"]
            ).PromptStore(tmp_path / "race-prompts"),
            provider_coordinator=Provider(),
            signer=PreviewSigner(secret=b"r" * 32),
        ),
        provider_coordinator=Provider(),
        task_coordinator=coordinator,
        publisher=publisher,
    )
    cleaner = HistoryCleaner(
        database,
        tmp_path / "race-audio",
        task_coordinator=coordinator,
    )

    retry = asyncio.create_task(service.retry_profile("history-1"))
    await publisher.entered.wait()
    clear = asyncio.create_task(cleaner.clear(confirm=True))
    await asyncio.sleep(0)
    assert not clear.done()
    publisher.release.set()
    result = await retry

    assert result.status == "completed"
    with pytest.raises(HistoryBusyError, match="profile rebuild"):
        await clear
    await database.dispose()


@pytest.mark.asyncio
async def test_credential_resume_discards_unpublished_checkpoints_but_fixed_rule_change_requires_fresh_preview(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService, ReanalysisStateError

    database = Database(tmp_path / "resume.sqlite3")
    await database.create_schema()
    await seed_history_run(
        database,
        batch_status="paused",
        item_statuses=("pending",),
    )
    async with database.session() as session:
        stale = AnalysisVersion(
            id="stale-version",
            source_job_id="job-0",
            batch_id="source-0",
            provider_id="kimi",
            model_id="kimi-k2.5",
            credential_generation=3,
            prompt_snapshot_json=json.dumps(PROMPTS),
            profile_snapshot_json="[]",
            fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            staged_results_json='{"meeting":{"private":"old-key"}}',
            priority=10,
            status="credential_changed",
            reanalysis_batch_id="history-1",
        )
        session.add(stale)
        item = await session.get(ReanalysisItem, "item-0")
        assert item is not None
        item.analysis_version_id = stale.id
        item.error_code = "credential_changed"
        await session.commit()
    provider = Provider(generation=4)
    prompts = __import__(
        "audio_memory.prompts.store", fromlist=["PromptStore"]
    ).PromptStore(tmp_path / "resume-prompts")
    prompts.initialize()
    service = ReanalysisService(
        database=database,
        preview_builder=ReanalysisPreviewBuilder(
            database=database,
            prompt_store=prompts,
            provider_coordinator=provider,
            signer=PreviewSigner(secret=b"r" * 32),
        ),
        provider_coordinator=provider,
    )

    resumed = await service.resume("history-1")
    async with database.session() as session:
        stale = await session.get(AnalysisVersion, "stale-version")
        item = await session.get(ReanalysisItem, "item-0")
        batch = await session.get(ReanalysisBatch, "history-1")
        assert item is not None and batch is not None
        assert item.analysis_version_id is None
        assert item.error_code is None
        batch.status = "paused"
        item.error_code = "fixed_rules_changed"
        await session.commit()
    assert resumed.status == "running"
    assert resumed.credential_generation == 4
    assert stale is None

    with pytest.raises(ReanalysisStateError, match="fresh preview"):
        await service.resume("history-1")
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking_reason", ["analysis_schema_changed", "transcript_changed"])
async def test_resume_rejects_any_later_blocking_item_reason(
    tmp_path: Path, blocking_reason: str
) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService, ReanalysisStateError

    database = Database(tmp_path / f"mixed-resume-{blocking_reason}.sqlite3")
    await database.create_schema()
    await seed_history_run(
        database,
        batch_status="paused",
        item_statuses=("pending", "pending"),
    )
    async with database.session() as session:
        first = await session.get(ReanalysisItem, "item-0")
        second = await session.get(ReanalysisItem, "item-1")
        assert first is not None and second is not None
        first.error_code = "model_response_invalid"
        second.error_code = blocking_reason
        await session.commit()
    provider = Provider(generation=3)
    prompts = __import__(
        "audio_memory.prompts.store", fromlist=["PromptStore"]
    ).PromptStore(tmp_path / f"mixed-prompts-{blocking_reason}")
    prompts.initialize()
    service = ReanalysisService(
        database=database,
        preview_builder=ReanalysisPreviewBuilder(
            database=database,
            prompt_store=prompts,
            provider_coordinator=provider,
            signer=PreviewSigner(secret=b"m" * 32),
        ),
        provider_coordinator=provider,
    )

    with pytest.raises(ReanalysisStateError, match="fresh preview"):
        await service.resume("history-1")

    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        first = await session.get(ReanalysisItem, "item-0")
        second = await session.get(ReanalysisItem, "item-1")
    assert batch is not None and batch.status == "paused"
    assert first is not None and first.error_code == "model_response_invalid"
    assert second is not None and second.error_code == blocking_reason
    await database.dispose()


class FailingAnalysisProvider:
    def __init__(self, error) -> None:
        self.error = error

    async def analyze_event_map(self, request, provider_snapshot):
        raise self.error

    async def analyze_scene(self, scene_id, request, provider_snapshot):
        raise AssertionError("event-map failure must stop scene calls")


class NeverProfileExtractor:
    async def extract(self, transcript, existing, provider_snapshot):
        raise AssertionError("event-map failure must stop profile extraction")


class NeverPublisher:
    async def publish(self, *args, **kwargs):
        raise AssertionError("failed analysis must not publish")


class StableGeneration:
    async def credential_generation(self, provider_id: str) -> int:
        return 3

    def publication_guard(self, provider_id: str):
        raise AssertionError("failed analysis must not reach publication")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pause_batch", "error_code", "expected_batch", "expected_item"),
    [
        (True, "insufficient_balance", "paused", "pending"),
        (False, "model_response_invalid", "running", "failed"),
    ],
)
async def test_provider_failures_pause_only_for_recoverable_account_state(
    tmp_path: Path,
    pause_batch: bool,
    error_code: str,
    expected_batch: str,
    expected_item: str,
) -> None:
    from audio_memory.analysis.provider import ProviderAnalysisError
    from audio_memory.analysis.runner import AnalysisRunner

    database = Database(tmp_path / f"failure-{error_code}.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("running",))
    async with database.session() as session:
        version = AnalysisVersion(
            id="new-version",
            source_job_id="job-0",
            batch_id="source-0",
            provider_id="kimi",
            model_id="kimi-k2.5",
            credential_generation=3,
            prompt_snapshot_json=json.dumps(PROMPTS),
            profile_snapshot_json="[]",
            fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            staged_results_json="{}",
            priority=10,
            status="running",
            reanalysis_batch_id="history-1",
        )
        session.add(version)
        item = await session.get(ReanalysisItem, "item-0")
        assert item is not None
        item.analysis_version_id = version.id
        await session.commit()
    runner = AnalysisRunner(
        database=database,
        provider=FailingAnalysisProvider(
            ProviderAnalysisError(
                "normalized failure",
                code=error_code,
                pause_batch=pause_batch,
            )
        ),
        profile_extractor=NeverProfileExtractor(),
        publisher=NeverPublisher(),
        generation_source=StableGeneration(),
    )

    with pytest.raises(ProviderAnalysisError):
        await runner.run("new-version")

    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        item = await session.get(ReanalysisItem, "item-0")
        version = await session.get(AnalysisVersion, "new-version")
    assert batch is not None and batch.status == expected_batch
    assert item is not None and item.status == expected_item
    assert item.error_code == error_code
    assert version is not None
    assert version.status == ("provider_paused" if pause_batch else "failed")
    await database.dispose()


@pytest.mark.asyncio
async def test_inflight_failure_cannot_overwrite_stop_or_start_the_next_item(
    tmp_path: Path,
) -> None:
    from audio_memory.analysis.provider import ProviderAnalysisError
    from audio_memory.analysis.runner import AnalysisRunner
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "stop-failure.sqlite3")
    await database.create_schema()
    await seed_history_run(database)
    async with database.session() as session:
        version = AnalysisVersion(
            id="stopping-version",
            source_job_id="job-1",
            batch_id="source-1",
            provider_id="kimi",
            model_id="kimi-k2.5",
            credential_generation=3,
            prompt_snapshot_json=json.dumps(PROMPTS),
            profile_snapshot_json="[]",
            fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            staged_results_json="{}",
            priority=10,
            status="running",
            reanalysis_batch_id="history-1",
        )
        session.add(version)
        batch = await session.get(ReanalysisBatch, "history-1")
        item = await session.get(ReanalysisItem, "item-0")
        assert batch is not None and item is not None
        batch.status = "stopping"
        item.status = "running"
        item.analysis_version_id = version.id
        await session.commit()
    runner = AnalysisRunner(
        database=database,
        provider=FailingAnalysisProvider(
            ProviderAnalysisError("ordinary", code="model_response_invalid")
        ),
        profile_extractor=NeverProfileExtractor(),
        publisher=NeverPublisher(),
        generation_source=StableGeneration(),
    )

    with pytest.raises(ProviderAnalysisError):
        await runner.run("stopping-version")
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=AnalysisTaskCoordinator(database),
        publisher=ProfilePublisher(database),
    )
    await worker.tick()

    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        items = list(
            await session.scalars(
                select(ReanalysisItem).order_by(ReanalysisItem.position)
            )
        )
        generated = list(
            await session.scalars(
                select(AnalysisVersion).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
        )
    assert batch is not None and batch.status == "stopped"
    assert [item.status for item in items] == ["failed", "stopped"]
    assert [version.id for version in generated] == ["stopping-version"]
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_binding", "expected_error"),
    [
        ("schema", "analysis_schema_changed"),
        ("transcript", "transcript_changed"),
    ],
)
async def test_worker_pauses_before_submission_when_persisted_compatibility_changes(
    tmp_path: Path, changed_binding: str, expected_error: str
) -> None:
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / f"binding-{changed_binding}.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("pending",))
    async with database.session() as session:
        if changed_binding == "schema":
            history = await session.get(ReanalysisBatch, "history-1")
            assert history is not None
            prompts = json.loads(history.prompt_snapshot_json)
            prompts["_reanalysis"]["fixed_rule_hashes"]["analysis_schemas"] = (
                "0" * 64
            )
            history.prompt_snapshot_json = json.dumps(prompts, sort_keys=True)
        else:
            transcript = await session.get(Transcript, "transcript-0")
            assert transcript is not None
            transcript.text = "same segment id, changed content"
        await session.commit()
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=AnalysisTaskCoordinator(database),
        publisher=ProfilePublisher(database),
    )

    await worker.tick()

    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        item = await session.get(ReanalysisItem, "item-0")
        generated = int(
            await session.scalar(
                select(func.count(AnalysisVersion.id)).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
            or 0
        )
    assert batch is not None and batch.status == "paused"
    assert item is not None and item.status == "pending"
    assert item.error_code == expected_error
    assert generated == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_schema_change_between_worker_check_and_queue_insert_pauses_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audio_memory.analysis.task_coordinator as coordinator_module
    from audio_memory.reanalysis.preview import current_fixed_rule_hashes
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "between-submit.sqlite3")
    await database.create_schema()
    await seed_history_run(database, item_statuses=("pending",))
    changed_hashes = current_fixed_rule_hashes()
    changed_hashes["analysis_schemas"] = "9" * 64
    monkeypatch.setattr(
        coordinator_module,
        "current_fixed_rule_hashes",
        lambda: changed_hashes,
        raising=False,
    )
    worker = ReanalysisWorker(
        database=database,
        task_coordinator=AnalysisTaskCoordinator(database),
        publisher=ProfilePublisher(database),
    )

    await worker.tick()

    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
        item = await session.get(ReanalysisItem, "item-0")
        generated = int(
            await session.scalar(
                select(func.count(AnalysisVersion.id)).where(
                    AnalysisVersion.reanalysis_batch_id == "history-1"
                )
            )
            or 0
        )
    assert batch is not None and batch.status == "paused"
    assert item is not None and item.status == "pending"
    assert item.error_code == "analysis_schema_changed"
    assert generated == 0
    await database.dispose()
