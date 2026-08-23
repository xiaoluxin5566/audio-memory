from __future__ import annotations

from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from audio_memory.db import Database
from audio_memory.models import AsrFileTask, utc_now


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

