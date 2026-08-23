from __future__ import annotations

from uuid import uuid4

import pytest

from audio_memory.asr.repository import AsrRepository
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, JobFile


async def seeded_database(tmp_path) -> tuple[Database, str, str]:
    database = Database(tmp_path / "asr.sqlite3")
    await database.create_schema()
    job_id, file_id = str(uuid4()), str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage="uploading"))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="private.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="a" * 64,
                position=0,
                temporary_path="/tmp/private.mp3",
            )
        )
        await session.commit()
    return database, job_id, file_id


@pytest.mark.asyncio
async def test_file_task_is_unique_and_paths_are_relative(tmp_path) -> None:
    database, job_id, file_id = await seeded_database(tmp_path)
    repository = AsrRepository(database)
    task = await repository.ensure_file_task(
        job_id=job_id,
        job_file_id=file_id,
        relative_source_path="audio/job/file.mp3",
        sha256="a" * 64,
    )
    duplicate = await repository.ensure_file_task(
        job_id=job_id,
        job_file_id=file_id,
        relative_source_path="audio/job/file.mp3",
        sha256="a" * 64,
    )

    assert duplicate.id == task.id
    assert duplicate.request_id == task.request_id
    with pytest.raises(ValueError, match="relative"):
        await repository.ensure_file_task(
            job_id=job_id,
            job_file_id=str(uuid4()),
            relative_source_path="/tmp/private.mp3",
            sha256="a" * 64,
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_confirmed_states_are_monotonic_and_idempotent(tmp_path) -> None:
    database, job_id, file_id = await seeded_database(tmp_path)
    repository = AsrRepository(database)
    task = await repository.ensure_file_task(
        job_id=job_id,
        job_file_id=file_id,
        relative_source_path="audio/job/file.mp3",
        sha256="a" * 64,
    )

    await repository.mark_storage_uploaded(task.id, "obj_random")
    await repository.mark_storage_uploaded(task.id, "obj_random")
    await repository.mark_submitted(task.id, task.request_id)
    await repository.mark_submitted(task.id, task.request_id)
    await repository.mark_completed(task.id, '{"result":{"utterances":[]}}')
    await repository.mark_completed(task.id, '{"result":{"utterances":[]}}')
    await repository.mark_materialized(task.id)
    await repository.mark_storage_deleted(task.id)

    restored = await repository.get(task.id)
    assert restored.status == "completed"
    assert restored.storage_status == "deleted"
    assert restored.remote_task_id == task.request_id
    assert restored.materialized_at is not None
    await database.dispose()

