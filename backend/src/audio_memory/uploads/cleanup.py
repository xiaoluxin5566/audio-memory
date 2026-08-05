from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, TempFileManifest


class UnsafeCleanupPathError(RuntimeError):
    pass


def assert_staging_path(path: Path, staging_root: Path) -> Path:
    resolved = path.resolve(strict=False)
    root = staging_root.resolve(strict=False)
    if resolved == root or root not in resolved.parents:
        raise UnsafeCleanupPathError("Cleanup target is outside the staging directory")
    return resolved


def remove_staged_file(path: Path, staging_root: Path) -> None:
    safe_path = assert_staging_path(path, staging_root)
    safe_path.unlink(missing_ok=True)


async def cleanup_abandoned_uploads(database: Database, staging_root: Path) -> int:
    cleaned = 0
    async with database.session() as session:
        records = list(
            await session.scalars(
                select(TempFileManifest)
                .join(AnalysisJob, AnalysisJob.id == TempFileManifest.task_uuid)
                .where(AnalysisJob.stage == JobStage.UPLOADING.value)
            )
        )
        abandoned_jobs: set[str] = set()
        for record in records:
            try:
                remove_staged_file(Path(record.file_path), staging_root)
            except UnsafeCleanupPathError:
                record.cleanup_status = "unsafe_path"
                continue
            abandoned_jobs.add(record.task_uuid)
            await session.delete(record)
            cleaned += 1
        for job_id in abandoned_jobs:
            job = await session.get(AnalysisJob, job_id)
            if job is not None:
                await session.delete(job)
        await session.commit()
    return cleaned
