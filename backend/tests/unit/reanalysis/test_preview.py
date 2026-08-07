from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    JobFile,
    ProfileFact,
    Transcript,
)
from audio_memory.prompts.store import PROMPT_SCENES, PromptStore
from audio_memory.providers.types import ProviderState, ProviderStateName


class AvailableProvider:
    def __init__(self) -> None:
        self.generation = 4
        self.model_id = "kimi-k2.5"
        self._lock = asyncio.Lock()

    async def snapshot_active_with_generation(self):
        return (
            ProviderState(
                provider_id="kimi",
                display_name="Kimi",
                model_id=self.model_id,
                active=True,
                state=ProviderStateName.AVAILABLE,
            ),
            self.generation,
        )

    async def validate_saved(self, provider_id: str):
        assert provider_id == "kimi"
        return type("Validation", (), {"ok": True})()

    @asynccontextmanager
    async def active_snapshot_guard(self):
        async with self._lock:
            yield await self.snapshot_active_with_generation()


class NoActiveProvider:
    async def snapshot_active_with_generation(self):
        raise LookupError("No active provider")


async def seed_completed_history(database: Database) -> None:
    async with database.session() as session:
        for batch_position, file_count in enumerate((2, 3, 2)):
            job_id = f"job-{batch_position}"
            batch_id = f"batch-{batch_position}"
            version_id = f"version-{batch_position}"
            session.add(AnalysisJob(id=job_id, stage="completed"))
            session.add(
                Batch(
                    id=batch_id,
                    job_id=job_id,
                    uploaded_at=f"2026-08-0{batch_position + 1}T08:00:00+00:00",
                    natural_date=f"2026-08-0{batch_position + 1}",
                )
            )
            for file_position in range(file_count):
                file_id = f"file-{batch_position}-{file_position}"
                session.add(
                    JobFile(
                        id=file_id,
                        job_id=job_id,
                        original_name=f"{file_id}.mp3",
                        extension=".mp3",
                        size_bytes=10,
                        sha256=(str(batch_position) + str(file_position)) * 32,
                        position=file_position,
                        temporary_path=f"/audio/{file_id}.mp3",
                    )
                )
                session.add(
                    Transcript(
                        id=f"transcript-{batch_position}-{file_position}",
                        job_file_id=file_id,
                        segment_index=0,
                        segment_uid=f"{file_id}:0",
                        start_ms=0,
                        end_ms=1000,
                        text="abcd",
                        words_json="[]",
                        risk_classified=True,
                    )
                )
        await session.flush()
        for batch_position in range(3):
            version_id = f"version-{batch_position}"
            batch_id = f"batch-{batch_position}"
            session.add(
                AnalysisVersion(
                    id=version_id,
                    source_job_id=f"job-{batch_position}",
                    batch_id=batch_id,
                    provider_id="kimi",
                    model_id="old-model",
                    credential_generation=1,
                    prompt_snapshot_json="{}",
                    profile_snapshot_json="[]",
                    fixed_rules_hash="f" * 64,
                    staged_results_json="{}",
                    priority=0,
                    status="completed",
                )
            )
        await session.flush()
        for batch_position in range(3):
            batch = await session.get(Batch, f"batch-{batch_position}")
            assert batch is not None
            batch.current_analysis_version_id = f"version-{batch_position}"
        session.add(
            ProfileFact(
                id="profile-1",
                subject_id="user",
                dimension="role",
                value_json='{"name":"builder"}',
                confidence=0.9,
                source_audio_json='["job-0"]',
                first_seen_at="2026-08-01T00:00:00+00:00",
                last_seen_at="2026-08-01T00:00:00+00:00",
                evidence_count=1,
                origin="explicit",
                status="active",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_preview_counts_history_and_never_estimates_local_audio_work(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder

    database = Database(tmp_path / "preview.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / "prompts")
    prompts.initialize()
    provider = AvailableProvider()
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    builder = ReanalysisPreviewBuilder(
        database=database,
        prompt_store=prompts,
        provider_coordinator=provider,
        signer=PreviewSigner(secret=b"test-secret" * 4, clock=lambda: now),
    )

    preview = await builder.build()

    assert preview.source_batch_count == 3
    assert preview.audio_file_count == 7
    assert preview.transcript_character_count == 28
    assert preview.estimated_calls_min == 18
    assert preview.estimated_calls_max == 42
    assert preview.whisper_calls == 0
    assert preview.diarization_calls == 0
    assert preview.provider_id == "kimi"
    assert preview.model_id == "kimi-k2.5"
    assert [item.batch_id for item in preview.snapshot.sources] == [
        "batch-2",
        "batch-1",
        "batch-0",
    ]
    assert set(preview.prompt_summary) == set(PROMPT_SCENES)
    assert preview.blockers == []
    await database.dispose()


@pytest.mark.asyncio
async def test_signed_preview_token_is_canonical_tamper_evident_and_expires(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import (
        PreviewSigner,
        PreviewTokenExpiredError,
        PreviewTokenInvalidError,
        ReanalysisPreviewBuilder,
    )

    database = Database(tmp_path / "signed.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / "prompts")
    prompts.initialize()
    provider = AvailableProvider()
    current = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    signer = PreviewSigner(
        secret=b"p" * 32,
        clock=lambda: current,
        ttl=timedelta(minutes=5),
    )
    builder = ReanalysisPreviewBuilder(
        database=database,
        prompt_store=prompts,
        provider_coordinator=provider,
        signer=signer,
    )
    preview = await builder.build()

    verified = signer.verify(preview.preview_token)
    assert verified["snapshot_hash"] == preview.snapshot_hash
    assert verified["source_batch_ids"] == ["batch-2", "batch-1", "batch-0"]
    assert verified["credential_generation"] == 4
    assert len(verified["fixed_rule_hashes"]["analysis_schemas"]) == 64
    assert verified["expires_at"] == "2026-08-06T12:05:00+00:00"

    encoded, signature = preview.preview_token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=="))
    payload["model_id"] = "forged-model"
    forged_encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    with pytest.raises(PreviewTokenInvalidError):
        signer.verify(f"{forged_encoded}.{signature}")

    current = current + timedelta(minutes=5, microseconds=1)
    with pytest.raises(PreviewTokenExpiredError):
        signer.verify(preview.preview_token)
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_component",
    ["prompt", "credential_generation", "history_scope", "profile"],
)
async def test_batch_creation_recomputes_every_cost_and_configuration_binding(
    tmp_path: Path, changed_component: str
) -> None:
    from sqlalchemy import delete, func, select, update

    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService, SnapshotChangedError

    database = Database(tmp_path / f"changed-{changed_component}.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / f"prompts-{changed_component}")
    prompts.initialize()
    provider = AvailableProvider()
    builder = ReanalysisPreviewBuilder(
        database=database,
        prompt_store=prompts,
        provider_coordinator=provider,
        signer=PreviewSigner(secret=b"s" * 32),
    )
    service = ReanalysisService(
        database=database,
        preview_builder=builder,
        provider_coordinator=provider,
    )
    preview = await service.preview()

    if changed_component == "prompt":
        current = prompts.get("meeting")
        prompts.save(
            "meeting", expected_version=current.version, content="changed meeting prompt"
        )
    elif changed_component == "credential_generation":
        provider.generation += 1
    elif changed_component == "history_scope":
        async with database.session() as session:
            await session.execute(delete(Batch).where(Batch.id == "batch-0"))
            await session.commit()
    else:
        async with database.session() as session:
            await session.execute(
                update(ProfileFact)
                .where(ProfileFact.id == "profile-1")
                .values(value_json='{"name":"changed"}')
            )
            await session.commit()

    with pytest.raises(SnapshotChangedError):
        await service.create_batch(preview.preview_token)

    async with database.session() as session:
        from audio_memory.models import ReanalysisBatch, ReanalysisItem

        assert await session.scalar(select(func.count(ReanalysisBatch.id))) == 0
        assert await session.scalar(select(func.count(ReanalysisItem.id))) == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_batch_creation_persists_frozen_snapshot_and_newest_first_items(
    tmp_path: Path,
) -> None:
    from sqlalchemy import select

    from audio_memory.models import ReanalysisBatch, ReanalysisItem
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService

    database = Database(tmp_path / "create.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / "create-prompts")
    prompts.initialize()
    provider = AvailableProvider()
    builder = ReanalysisPreviewBuilder(
        database=database,
        prompt_store=prompts,
        provider_coordinator=provider,
        signer=PreviewSigner(secret=b"c" * 32),
    )
    service = ReanalysisService(
        database=database,
        preview_builder=builder,
        provider_coordinator=provider,
    )

    preview = await service.preview()
    created = await service.create_batch(preview.preview_token)

    async with database.session() as session:
        stored = await session.get(ReanalysisBatch, created.id)
        items = list(
            await session.scalars(
                select(ReanalysisItem)
                .where(ReanalysisItem.reanalysis_batch_id == created.id)
                .order_by(ReanalysisItem.position)
            )
        )
    assert stored is not None and stored.status == "pending"
    assert stored.snapshot_hash == preview.snapshot_hash
    assert stored.credential_generation == 4
    persisted_prompts = json.loads(stored.prompt_snapshot_json)
    assert (
        persisted_prompts["_reanalysis"]["fixed_rule_hashes"]
        == preview.snapshot.fixed_rule_hashes
    )
    assert set(persisted_prompts["_reanalysis"]["transcript_fingerprints"]) == {
        "batch-0",
        "batch-1",
        "batch-2",
    }
    assert json.loads(stored.profile_snapshot_json) == list(preview.snapshot.profile_snapshot)
    assert [item.source_batch_id for item in items] == [
        "batch-2",
        "batch-1",
        "batch-0",
    ]
    assert {item.status for item in items} == {"pending"}
    await database.dispose()


@pytest.mark.asyncio
async def test_identical_prompt_content_with_new_version_rejects_old_preview(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService, SnapshotChangedError

    database = Database(tmp_path / "prompt-version.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / "version-prompts")
    prompts.initialize()
    provider = AvailableProvider()
    service = ReanalysisService(
        database=database,
        preview_builder=ReanalysisPreviewBuilder(
            database=database,
            prompt_store=prompts,
            provider_coordinator=provider,
            signer=PreviewSigner(secret=b"v" * 32),
        ),
        provider_coordinator=provider,
    )
    preview = await service.preview()
    current = prompts.get("meeting")
    prompts.save(
        "meeting",
        expected_version=current.version,
        content=current.content,
    )

    with pytest.raises(SnapshotChangedError):
        await service.create_batch(preview.preview_token)
    await database.dispose()


@pytest.mark.asyncio
async def test_creation_fences_mutation_after_snapshot_read_until_commit(
    tmp_path: Path,
) -> None:
    from sqlalchemy import select, update

    from audio_memory.models import ReanalysisBatch
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import ReanalysisService

    database = Database(tmp_path / "atomic-create.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / "atomic-prompts")
    prompts.initialize()
    provider = AvailableProvider()
    builder = ReanalysisPreviewBuilder(
        database=database,
        prompt_store=prompts,
        provider_coordinator=provider,
        signer=PreviewSigner(secret=b"z" * 32),
    )
    service = ReanalysisService(
        database=database,
        preview_builder=builder,
        provider_coordinator=provider,
    )
    preview = await service.preview()
    snapshot_read = asyncio.Event()
    allow_creation = asyncio.Event()
    original_build = builder.build

    async def paused_build(*, provider_binding=None):
        result = await original_build(provider_binding=provider_binding)
        if provider_binding is not None:
            snapshot_read.set()
            await allow_creation.wait()
        return result

    builder.build = paused_build  # type: ignore[method-assign]
    creation = asyncio.create_task(service.create_batch(preview.preview_token))
    await snapshot_read.wait()
    current = prompts.get("meeting")
    mutation = asyncio.create_task(
        asyncio.to_thread(
            prompts.save,
            "meeting",
            expected_version=current.version,
            content="mutation attempted inside creation window",
        )
    )

    async def mutate_profile() -> None:
        async with database.session() as session:
            await session.execute(
                update(ProfileFact)
                .where(ProfileFact.id == "profile-1")
                .values(value_json='{"name":"raced"}')
            )
            await session.commit()

    profile_mutation = asyncio.create_task(mutate_profile())
    await asyncio.sleep(0)
    assert not mutation.done()
    assert not profile_mutation.done()

    allow_creation.set()
    created = await creation
    await mutation
    await profile_mutation

    async with database.session() as session:
        stored = await session.scalar(
            select(ReanalysisBatch).where(ReanalysisBatch.id == created.id)
        )
    assert stored is not None
    persisted = json.loads(stored.prompt_snapshot_json)
    assert persisted["meeting"]["content"] == current.content
    assert json.loads(stored.profile_snapshot_json)[0]["value"] == {
        "name": "builder"
    }
    assert prompts.get("meeting").content == "mutation attempted inside creation window"
    await database.dispose()


@pytest.mark.asyncio
async def test_preview_keeps_history_counts_when_no_provider_is_active(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder

    database = Database(tmp_path / "no-provider.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / "no-provider-prompts")
    prompts.initialize()
    builder = ReanalysisPreviewBuilder(
        database=database,
        prompt_store=prompts,
        provider_coordinator=NoActiveProvider(),
        signer=PreviewSigner(secret=b"n" * 32),
    )

    preview = await builder.build()

    assert preview.source_batch_count == 3
    assert preview.audio_file_count == 7
    assert preview.provider_id == ""
    assert preview.model_id == ""
    assert preview.blockers == ["no_active_provider"]
    await database.dispose()


@pytest.mark.asyncio
async def test_no_provider_preview_create_is_a_documented_blocker(
    tmp_path: Path,
) -> None:
    from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
    from audio_memory.reanalysis.service import PreviewBlockedError, ReanalysisService

    database = Database(tmp_path / "no-provider-create.sqlite3")
    await database.create_schema()
    await seed_completed_history(database)
    prompts = PromptStore(tmp_path / "no-provider-create-prompts")
    prompts.initialize()
    provider = NoActiveProvider()
    service = ReanalysisService(
        database=database,
        preview_builder=ReanalysisPreviewBuilder(
            database=database,
            prompt_store=prompts,
            provider_coordinator=provider,
            signer=PreviewSigner(secret=b"b" * 32),
        ),
        provider_coordinator=provider,
    )
    preview = await service.preview()

    with pytest.raises(PreviewBlockedError) as blocked:
        await service.create_batch(preview.preview_token)
    assert blocked.value.blockers == ["no_active_provider"]
    await database.dispose()
