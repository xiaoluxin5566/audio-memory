from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, update

from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.analysis.versions import AnalysisSnapshot
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    ProviderMetadata,
)


@dataclass(frozen=True, slots=True)
class BatchView:
    id: str
    card_count: int


class BatchRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_job(self, *, stage: str) -> AnalysisJob:
        try:
            normalized_stage = JobStage(stage).value
        except ValueError as exc:
            raise ValueError(f"Unsupported job stage: {stage}") from exc

        job = AnalysisJob(id=str(uuid4()), stage=normalized_stage)
        async with self.database.session() as session:
            session.add(job)
            await session.commit()
            await session.refresh(job)
        return job

    async def get_job(self, job_id: str) -> AnalysisJob:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            return job

    async def stage_card(self, job_id: str, card: dict[str, object]) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            staged = json.loads(job.staged_results_json)
            staged.append(card)
            job.staged_results_json = json.dumps(staged, ensure_ascii=False)
            await session.commit()

    async def publish_batch(self, job_id: str, *, batch_id: str) -> BatchView:
        async with self.database.session() as session:
            async with session.begin():
                job = await session.get(AnalysisJob, job_id)
                if job is None:
                    raise LookupError(f"Unknown analysis job: {job_id}")
                if job.stage != JobStage.READY_TO_COMMIT.value:
                    raise ValueError(f"Job is not ready to publish: {job.stage}")

                now = datetime.now(UTC)
                batch = Batch(
                    id=batch_id,
                    job_id=job.id,
                    provider_id=job.provider_id,
                    model_id=job.model_id,
                    uploaded_at=now.isoformat(),
                    natural_date=now.date().isoformat(),
                )
                session.add(batch)
                staged_cards = json.loads(job.staged_results_json)
                for draft in staged_cards:
                    session.add(
                        Card(
                            id=str(draft["id"]),
                            batch_id=batch.id,
                            scene_id=str(draft["scene_id"]),
                            position=int(draft["position"]),
                            payload_json=json.dumps(
                                draft["payload"], ensure_ascii=False
                            ),
                        )
                    )
                job.stage = JobStage.COMPLETED.value
                await session.flush()

            return BatchView(id=batch.id, card_count=len(staged_cards))

    async def list_feed_batches(self) -> list[BatchView]:
        async with self.database.session() as session:
            rows = await session.execute(
                select(Batch.id, func.count(Card.id))
                .outerjoin(Card, Card.batch_id == Batch.id)
                .group_by(Batch.id)
                .order_by(Batch.uploaded_at.desc())
            )
            return [BatchView(id=batch_id, card_count=count) for batch_id, count in rows]


class AnalysisVersionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_attempt(
        self,
        *,
        source_job_id: str,
        batch_id: str | None,
        snapshot: AnalysisSnapshot,
        reanalysis_batch_id: str | None,
    ) -> AnalysisVersion:
        if not source_job_id.strip():
            raise ValueError("source_job_id must not be empty")

        version = AnalysisVersion(
            id=str(uuid4()),
            source_job_id=source_job_id,
            batch_id=batch_id,
            provider_id=snapshot.provider_id,
            model_id=snapshot.model_id,
            credential_generation=snapshot.credential_generation,
            prompt_snapshot_json=json.dumps(
                snapshot.prompt_snapshot, ensure_ascii=False, sort_keys=True
            ),
            profile_snapshot_json=json.dumps(
                snapshot.profile_snapshot, ensure_ascii=False, sort_keys=True
            ),
            fixed_rules_hash=snapshot.fixed_rules_hash,
            staged_results_json="{}",
            status="running",
            reanalysis_batch_id=reanalysis_batch_id,
        )
        async with self.database.session() as session:
            session.add(version)
            await session.commit()
            await session.refresh(version)
        return version

    async def mark_current(self, *, batch_id: str, version_id: str) -> None:
        async with self.database.session() as session:
            async with session.begin():
                version = await session.get(AnalysisVersion, version_id)
                if version is None:
                    raise LookupError(f"Unknown analysis version: {version_id}")
                if version.batch_id != batch_id:
                    raise ValueError("Analysis version does not belong to target batch")
                if version.status != "completed":
                    raise ValueError("Analysis version must be completed")
                batch = await session.get(Batch, batch_id)
                if batch is None:
                    raise LookupError(f"Unknown batch: {batch_id}")
                batch.current_analysis_version_id = version.id

    async def current_for_batch(self, batch_id: str) -> AnalysisVersion | None:
        async with self.database.session() as session:
            batch = await session.get(Batch, batch_id)
            if batch is None or batch.current_analysis_version_id is None:
                return None
            return await session.get(
                AnalysisVersion, batch.current_analysis_version_id
            )


class ProviderMetadataRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def ensure_defaults(self, model_ids: dict[str, str]) -> None:
        async with self.database.session() as session:
            async with session.begin():
                for provider_id, model_id in model_ids.items():
                    row = await session.get(ProviderMetadata, provider_id)
                    if row is None:
                        session.add(
                            ProviderMetadata(
                                provider_id=provider_id,
                                default_model_id=model_id,
                            )
                        )
                    else:
                        row.default_model_id = model_id

    async def list_all(self) -> list[ProviderMetadata]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(ProviderMetadata).order_by(ProviderMetadata.provider_id)
            )
            return list(rows)

    async def activate(self, provider_id: str) -> None:
        async with self.database.session() as session:
            async with session.begin():
                target = await session.get(ProviderMetadata, provider_id)
                if target is None:
                    raise LookupError(f"Unknown provider: {provider_id}")
                if target.active:
                    return
                await session.execute(update(ProviderMetadata).values(active=False))
                target.active = True

    async def update_validation(
        self,
        provider_id: str,
        *,
        status: str,
        validated_at: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        async with self.database.session() as session:
            async with session.begin():
                row = await session.get(ProviderMetadata, provider_id)
                if row is None:
                    raise LookupError(f"Unknown provider: {provider_id}")
                row.validation_status = status
                row.last_validated_at = validated_at
                row.last_validation_error_code = error_code
                row.last_validation_error_message = error_message
