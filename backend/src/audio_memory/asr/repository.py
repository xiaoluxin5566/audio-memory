from __future__ import annotations

from pathlib import PurePosixPath
import json
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AsrFileTask, Transcript, utc_now
from audio_memory.transcription.segments import TranscriptSegment


class AsrRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def ensure_file_task(
        self,
        *,
        job_id: str,
        job_file_id: str,
        relative_source_path: str,
        sha256: str,
    ) -> AsrFileTask:
        self._validate_relative_path(relative_source_path)
        async with self.database.session() as session:
            existing = await session.scalar(
                select(AsrFileTask).where(AsrFileTask.job_file_id == job_file_id)
            )
            if existing is not None:
                return existing
            task = AsrFileTask(
                id=str(uuid4()),
                job_id=job_id,
                job_file_id=job_file_id,
                relative_source_path=relative_source_path,
                sha256=sha256,
                request_id=str(uuid4()),
            )
            session.add(task)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(AsrFileTask).where(
                        AsrFileTask.job_file_id == job_file_id
                    )
                )
                if existing is None:
                    raise
                return existing
            await session.refresh(task)
            return task

    async def get(self, task_id: str) -> AsrFileTask:
        async with self.database.session() as session:
            task = await session.get(AsrFileTask, task_id)
            if task is None:
                raise LookupError(task_id)
            return task

    async def recoverable_job_ids(self) -> list[str]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(AnalysisJob.id)
                .where(
                    or_(
                        and_(
                            AnalysisJob.stage == "transcribing",
                            AnalysisJob.id.in_(select(AsrFileTask.job_id)),
                        ),
                        and_(
                            AnalysisJob.stage == "failed",
                            AnalysisJob.error_code == "managed_storage_unavailable",
                        ),
                    )
                )
                .distinct()
                .order_by(AnalysisJob.created_at, AnalysisJob.id)
            )
            return list(rows)

    async def mark_storage_uploaded(self, task_id: str, object_id: str) -> None:
        async with self.database.session() as session:
            task = await self._require(session, task_id)
            if task.storage_status in {"uploaded", "deleted"}:
                if task.storage_object_id != object_id:
                    raise ValueError("storage object mismatch")
                return
            task.storage_object_id = object_id
            task.storage_status = "uploaded"
            task.updated_at = utc_now()
            await session.commit()

    async def mark_submitted(self, task_id: str, remote_task_id: str) -> None:
        async with self.database.session() as session:
            task = await self._require(session, task_id)
            if task.remote_task_id is not None:
                if task.remote_task_id != remote_task_id:
                    raise ValueError("remote task mismatch")
                return
            task.remote_task_id = remote_task_id
            task.status = "submitted"
            task.updated_at = utc_now()
            await session.commit()

    async def mark_completed(self, task_id: str, result_json: str) -> None:
        async with self.database.session() as session:
            task = await self._require(session, task_id)
            if task.status == "completed":
                if task.result_json != result_json:
                    raise ValueError("completed result mismatch")
                return
            if task.remote_task_id is None:
                raise ValueError("task must be submitted before completion")
            task.result_json = result_json
            task.status = "completed"
            task.error_code = None
            task.updated_at = utc_now()
            await session.commit()

    async def mark_materialized(self, task_id: str) -> None:
        async with self.database.session() as session:
            task = await self._require(session, task_id)
            if task.materialized_at is not None:
                return
            if task.status != "completed" or task.result_json is None:
                raise ValueError("only completed tasks can be materialized")
            task.materialized_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()

    async def materialize(
        self, task_id: str, segments: list[TranscriptSegment]
    ) -> None:
        async with self.database.session() as session:
            task = await self._require(session, task_id)
            if task.materialized_at is not None:
                return
            if task.status != "completed" or task.result_json is None:
                raise ValueError("only completed tasks can be materialized")
            for segment in segments:
                if segment.file_id != task.job_file_id:
                    raise ValueError("segment file mismatch")
                session.add(
                    Transcript(
                        id=str(uuid4()),
                        job_file_id=segment.file_id,
                        segment_index=segment.index,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        text=segment.text,
                        words_json=json.dumps(segment.words, ensure_ascii=False),
                        speaker_id=getattr(segment, "speaker_id", None) or "unknown",
                        risk_classified=False,
                        is_reliable=True,
                        reliability_weight=1.0,
                    )
                )
            task.materialized_at = utc_now()
            task.updated_at = utc_now()
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                refreshed = await session.get(AsrFileTask, task_id)
                if refreshed is None or refreshed.materialized_at is None:
                    raise

    async def mark_storage_deleted(self, task_id: str) -> None:
        async with self.database.session() as session:
            task = await self._require(session, task_id)
            if task.storage_status == "deleted":
                return
            if task.materialized_at is None:
                raise ValueError("storage cannot be deleted before materialization")
            task.storage_status = "deleted"
            task.updated_at = utc_now()
            await session.commit()

    async def mark_submission_unknown(self, task_id: str, error_code: str) -> None:
        async with self.database.session() as session:
            task = await self._require(session, task_id)
            if task.remote_task_id is not None or task.status == "completed":
                return
            task.status = "submission_unknown"
            task.error_code = error_code
            task.updated_at = utc_now()
            await session.commit()

    @staticmethod
    async def _require(session, task_id: str) -> AsrFileTask:
        task = await session.get(AsrFileTask, task_id)
        if task is None:
            raise LookupError(task_id)
        return task

    @staticmethod
    def _validate_relative_path(value: str) -> None:
        path = PurePosixPath(value)
        if path.is_absolute() or not value or ".." in path.parts:
            raise ValueError("source path must be relative to the runtime data root")
