from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4
from pathlib import Path

from sqlalchemy import select

from audio_memory.analysis.profile import ProfileDelta
from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import (
    AnalysisJob,
    Batch,
    Card,
    JobFile,
    ProfileFact,
    TempFileManifest,
    Todo,
)
from audio_memory.config import AppPaths
from audio_memory.prompts.schemas import SceneResult


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
        job_id: str,
        results: list[SceneResult],
        profile_delta: list[ProfileDelta],
    ) -> AnalysisOutcome:
        by_scene = {result.scene_id: result for result in results}
        visible = [
            by_scene[scene_id]
            for scene_id in CARD_ORDER
            if scene_id in by_scene and by_scene[scene_id].should_generate
        ]
        todos = [todo for result in results for todo in result.todos]
        batch_id = str(uuid4())
        now = datetime.now(UTC)
        moved: list[tuple[Path, Path]] = []
        destination_root = self.paths.audio / batch_id if self.paths else None
        try:
            async with self.database.session() as session:
                if destination_root is not None:
                    destination_root.mkdir(mode=0o700, parents=True, exist_ok=False)
                    files = list(
                        await session.scalars(
                            select(JobFile)
                            .where(JobFile.job_id == job_id)
                            .order_by(JobFile.position)
                        )
                    )
                    for source_file in files:
                        source = Path(source_file.temporary_path)
                        destination = destination_root / (
                            f"{source_file.id}{source_file.extension}"
                        )
                        os.replace(source, destination)
                        moved.append((source, destination))
                        source_file.temporary_path = str(destination)
                    manifests = list(
                        await session.scalars(
                            select(TempFileManifest).where(
                                TempFileManifest.task_uuid == job_id
                            )
                        )
                    )
                    for manifest in manifests:
                        await session.delete(manifest)
                job = await session.get(AnalysisJob, job_id)
                if job is None:
                    raise LookupError(f"Unknown analysis job: {job_id}")
                session.add(
                    Batch(
                        id=batch_id,
                        job_id=job_id,
                        provider_id=job.provider_id,
                        model_id=job.model_id,
                        uploaded_at=now.isoformat(),
                        natural_date=now.date().isoformat(),
                    )
                )
                for position, result in enumerate(visible):
                    session.add(
                        Card(
                            id=str(uuid4()),
                            batch_id=batch_id,
                            scene_id=result.scene_id,
                            position=position,
                            payload_json=json.dumps(
                                result.model_dump(mode="json"), ensure_ascii=False
                            ),
                        )
                    )
                for draft in todos:
                    session.add(
                        Todo(
                            id=str(uuid4()),
                            batch_id=batch_id,
                            text=draft.text,
                            due_at=draft.due_at,
                        )
                    )
                for fact in profile_delta:
                    session.add(
                        ProfileFact(
                            id=str(uuid4()),
                            subject_id=fact.subject_id,
                            dimension=fact.dimension,
                            value_json=json.dumps(fact.value, ensure_ascii=False),
                            confidence=fact.confidence,
                            source_audio_json=json.dumps({"job_id": job_id}),
                            first_seen_at=now.isoformat(),
                            last_seen_at=now.isoformat(),
                            evidence_count=1,
                            origin="explicit" if fact.explicit else "inferred",
                            status="active",
                        )
                    )
                job.stage = JobStage.COMPLETED.value
                job.error_code = None
                await session.commit()
        except BaseException:
            for source, destination in reversed(moved):
                if destination.exists():
                    source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.replace(destination, source)
            if destination_root is not None:
                try:
                    destination_root.rmdir()
                except OSError:
                    pass
            raise
        return AnalysisOutcome(batch_id, len(visible), len(todos))
