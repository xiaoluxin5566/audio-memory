from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5
from pathlib import Path

from sqlalchemy import func, select, update

from audio_memory.analysis.profile import ProfileDelta
from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    ProfileFact,
    ReanalysisBatch,
    ReanalysisItem,
    TempFileManifest,
    Todo,
)
from audio_memory.config import AppPaths
from audio_memory.prompts.schemas import SceneResultBase


CARD_ORDER = ("meeting", "parenting", "content", "growth", "inspiration")


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    batch_id: str
    card_count: int
    todo_count: int


class AnalysisPublisher:
    def __init__(self, database: Database, paths: AppPaths | None = None) -> None:
        self.database = database
        self.paths = paths

    async def publish(
        self,
        version_id: str,
        results: list[SceneResultBase],
        profile_delta: list[ProfileDelta],
        *,
        worker_owner_id: str | None = None,
    ) -> AnalysisOutcome:
        by_scene = {result.scene_id: result for result in results}
        visible = [
            by_scene[scene_id]
            for scene_id in CARD_ORDER
            if scene_id in by_scene and by_scene[scene_id].should_generate
        ]
        todos = [todo for result in results for todo in result.todos]
        now = datetime.now(UTC)
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            if version is None:
                raise LookupError(f"Unknown analysis version: {version_id}")
            job_id = version.source_job_id
            batch_id = version.batch_id or str(
                uuid5(NAMESPACE_URL, f"audio-memory-batch:{version.id}")
            )
            if version.status == "completed":
                return AnalysisOutcome(
                    batch_id,
                    int(
                        await session.scalar(
                            select(func.count(Card.id)).where(
                                Card.analysis_version_id == version.id
                            )
                        )
                        or 0
                    ),
                    int(
                        await session.scalar(
                            select(func.count(Todo.id)).where(
                                Todo.analysis_version_id == version.id
                            )
                        )
                        or 0
                    ),
                )
            if (
                version.status != "running"
                or (
                    worker_owner_id is not None
                    and version.worker_owner_id != worker_owner_id
                )
            ):
                raise RuntimeError("Analysis worker lease was lost before publication")
            files = list(
                await session.scalars(
                    select(JobFile)
                    .where(JobFile.job_id == job_id)
                    .order_by(JobFile.position)
                )
            )

        destination_root = (
            self.paths.audio / batch_id
            if self.paths is not None and version.batch_id is None
            else None
        )
        destinations: dict[str, str] = {}
        if destination_root is not None:
            destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for source_file in files:
                source = Path(source_file.temporary_path)
                destination = destination_root / (
                    f"{source_file.id}{source_file.extension}"
                )
                if source != destination and source.exists() and not destination.exists():
                    os.replace(source, destination)
                if not destination.exists():
                    raise FileNotFoundError(f"Missing publication audio: {source}")
                destinations[source_file.id] = str(destination)

        async with self.database.session() as session:
            async with session.begin():
                if worker_owner_id is not None:
                    fenced = await session.execute(
                        update(AnalysisVersion)
                        .where(
                            AnalysisVersion.id == version_id,
                            AnalysisVersion.status == "running",
                            AnalysisVersion.worker_owner_id == worker_owner_id,
                        )
                        .values(worker_owner_id=worker_owner_id)
                    )
                    if int(fenced.rowcount) != 1:
                        raise RuntimeError(
                            "Analysis worker lease was lost before publication"
                        )
                version = await session.get(AnalysisVersion, version_id)
                if version is None:
                    raise LookupError(f"Unknown analysis version: {version_id}")
                if version.status == "completed":
                    return AnalysisOutcome(batch_id, len(visible), len(todos))
                job = await session.get(AnalysisJob, version.source_job_id)
                if job is None:
                    raise LookupError(f"Unknown analysis job: {version.source_job_id}")
                batch = await session.get(Batch, batch_id)
                if batch is None:
                    batch = Batch(
                        id=batch_id,
                        job_id=job.id,
                        provider_id=version.provider_id,
                        model_id=version.model_id,
                        uploaded_at=now.isoformat(),
                        natural_date=now.date().isoformat(),
                    )
                    session.add(batch)
                    await session.flush()
                for source_file in files:
                    stored_file = await session.get(JobFile, source_file.id)
                    if stored_file is not None and source_file.id in destinations:
                        stored_file.temporary_path = destinations[source_file.id]
                manifests = list(
                    await session.scalars(
                        select(TempFileManifest).where(
                            TempFileManifest.task_uuid == job.id
                        )
                    )
                )
                for manifest in manifests:
                    await session.delete(manifest)
                for position, result in enumerate(visible):
                    card_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"audio-memory-card:{version.id}:{result.scene_id}:{position}",
                        )
                    )
                    if await session.get(Card, card_id) is None:
                        session.add(
                            Card(
                                id=card_id,
                                batch_id=batch_id,
                                analysis_version_id=version.id,
                                scene_id=result.scene_id,
                                position=position,
                                payload_json=json.dumps(
                                    result.model_dump_for_frontend(),
                                    ensure_ascii=False,
                                ),
                            )
                        )
                for position, draft in enumerate(todos):
                    todo_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"audio-memory-todo:{version.id}:{position}",
                        )
                    )
                    if await session.get(Todo, todo_id) is None:
                        session.add(
                            Todo(
                                id=todo_id,
                                batch_id=batch_id,
                                analysis_version_id=version.id,
                                source_job_id=job.id,
                                source_event_id=draft.source_event_id,
                                evidence_segment_ids_json=json.dumps(
                                    draft.evidence_segment_ids,
                                    ensure_ascii=False,
                                ),
                                normalized_action=draft.action,
                                normalized_assignee=draft.assignee_text,
                                text=draft.text,
                                due_at=(
                                    draft.due_at.isoformat()
                                    if draft.due_at is not None
                                    else None
                                ),
                            )
                        )
                for position, fact in enumerate(profile_delta):
                    fact_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"audio-memory-profile:{version.id}:{position}",
                        )
                    )
                    if await session.get(ProfileFact, fact_id) is None:
                        session.add(
                            ProfileFact(
                                id=fact_id,
                                subject_id=fact.subject_id,
                                dimension=fact.dimension,
                                value_json=json.dumps(fact.value, ensure_ascii=False),
                                confidence=fact.confidence,
                                source_audio_json=json.dumps({"job_id": job.id}),
                                first_seen_at=now.isoformat(),
                                last_seen_at=now.isoformat(),
                                evidence_count=1,
                                origin="explicit" if fact.explicit else "inferred",
                                status="active",
                            )
                        )
                version.batch_id = batch.id
                version.status = "completed"
                version.error_code = None
                version.completed_at = now.isoformat()
                batch.current_analysis_version_id = version.id
                batch.provider_id = version.provider_id
                batch.model_id = version.model_id
                job.provider_id = version.provider_id
                job.model_id = version.model_id
                job.stage = JobStage.COMPLETED.value
                job.error_code = None
                if version.reanalysis_batch_id is not None:
                    item = await session.scalar(
                        select(ReanalysisItem).where(
                            ReanalysisItem.analysis_version_id == version.id
                        )
                    )
                    if item is not None:
                        item.status = "completed"
                        item.error_code = None
                        item.completed_at = now.isoformat()
                    remaining = await session.scalar(
                        select(ReanalysisItem.id)
                        .where(
                            ReanalysisItem.reanalysis_batch_id
                            == version.reanalysis_batch_id,
                            ReanalysisItem.status.in_(("pending", "running")),
                            ReanalysisItem.id != (item.id if item is not None else ""),
                        )
                        .limit(1)
                    )
                    if remaining is None:
                        history = await session.get(
                            ReanalysisBatch, version.reanalysis_batch_id
                        )
                        if history is not None:
                            history.status = "completed"
                            history.completed_at = now.isoformat()
        return AnalysisOutcome(batch_id, len(visible), len(todos))
