from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from sqlalchemy import select

from audio_memory.db import Database
from audio_memory.config import PinnedDevelopmentRoot
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


def remove_staged_file(
    path: Path,
    staging_root: Path,
    *,
    write_boundary: PinnedDevelopmentRoot | None = None,
) -> None:
    assert_staging_path(path, staging_root)
    safe_path = Path(os.path.abspath(path.expanduser()))
    try:
        metadata = safe_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeCleanupPathError("Cleanup target is not a regular file")
    if write_boundary is None:
        safe_path.unlink(missing_ok=True)
    else:
        write_boundary.unlink_file(path, missing_ok=True)


def remove_staged_entry(
    path: Path,
    staging_root: Path,
    *,
    write_boundary: PinnedDevelopmentRoot | None = None,
) -> None:
    assert_staging_path(path, staging_root)
    safe_path = Path(os.path.abspath(path.expanduser()))
    try:
        metadata = safe_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(metadata.st_mode):
        remove_staged_file(
            path,
            staging_root,
            write_boundary=write_boundary,
        )
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeCleanupPathError("Cleanup target is not a regular file or directory")
    if write_boundary is None:
        shutil.rmtree(safe_path)
    else:
        write_boundary.remove_directory_tree(path, missing_ok=True)


def move_staged_directory(
    source: Path,
    destination: Path,
    staging_root: Path,
    *,
    write_boundary: PinnedDevelopmentRoot | None = None,
) -> bool:
    assert_staging_path(source, staging_root)
    assert_staging_path(destination, staging_root)
    safe_source = Path(os.path.abspath(source.expanduser()))
    safe_destination = Path(os.path.abspath(destination.expanduser()))
    try:
        metadata = safe_source.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeCleanupPathError("Cleanup source is not a directory")
    try:
        safe_destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise UnsafeCleanupPathError("Cleanup destination already exists")
    if write_boundary is None:
        safe_source.rename(safe_destination)
    else:
        write_boundary.move_directory(source, destination)
    return True


async def cleanup_abandoned_uploads(
    database: Database,
    staging_root: Path,
    *,
    write_boundary: PinnedDevelopmentRoot | None = None,
) -> int:
    cleaned = 0
    async with database.session() as session:
        upload_records = list(
            await session.scalars(
                select(TempFileManifest)
                .join(AnalysisJob, AnalysisJob.id == TempFileManifest.task_uuid)
                .where(AnalysisJob.stage == JobStage.UPLOADING.value)
            )
        )
        cancelled_records = list(
            await session.scalars(
                select(TempFileManifest).where(
                    TempFileManifest.cleanup_status == "cancelled_pending"
                )
            )
        )
        records = upload_records + [
            record for record in cancelled_records if record not in upload_records
        ]
        abandoned_jobs: set[str] = set()
        for record in records:
            try:
                if record.cleanup_status == "cancelled_pending":
                    remove_staged_entry(
                        Path(record.file_path),
                        staging_root,
                        write_boundary=write_boundary,
                    )
                else:
                    remove_staged_file(
                        Path(record.file_path),
                        staging_root,
                        write_boundary=write_boundary,
                    )
            except UnsafeCleanupPathError:
                record.cleanup_status = "unsafe_path"
                continue
            if record.cleanup_status != "cancelled_pending":
                abandoned_jobs.add(record.task_uuid)
            await session.delete(record)
            cleaned += 1
        for job_id in abandoned_jobs:
            job = await session.get(AnalysisJob, job_id)
            if job is not None:
                await session.delete(job)
        await session.commit()
    return cleaned
