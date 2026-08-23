from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from audio_memory.asr.client import VolcanoPollResult
from audio_memory.asr.coordinator import VolcanoAsrCoordinator
from audio_memory.asr.repository import AsrRepository
from audio_memory.asr.storage import ReadTicket, UploadTicket
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, JobFile, Transcript
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class Keychain:
    def read(self, credential_id: str) -> KeychainReadResult:
        return KeychainReadResult(KeychainStatus.CONFIGURED, b"asr-secret")


class Storage:
    def __init__(self) -> None:
        self.created = 0
        self.uploaded = 0
        self.read_urls = 0
        self.deleted = 0

    async def create_upload(self, request):
        self.created += 1
        return UploadTicket(
            object_id="obj_random",
            upload_url="https://bucket.example/object?sig=upload",
            upload_headers={"Content-Type": request.content_type},
            expires_at=datetime(2026, 8, 24, tzinfo=UTC),
        )

    async def upload_file(self, ticket, source: Path) -> None:
        self.uploaded += 1
        assert source.read_bytes() == b"original-audio"

    async def create_read_url(self, object_id: str) -> ReadTicket:
        self.read_urls += 1
        assert object_id == "obj_random"
        return ReadTicket(
            url="https://bucket.example/object?sig=read",
            expires_at=datetime(2026, 8, 24, tzinfo=UTC),
        )

    async def delete(self, object_id: str) -> None:
        self.deleted += 1


class Volcano:
    def __init__(self) -> None:
        self.submits = 0
        self.polls = 0

    async def submit(self, *, api_key: bytes, request) -> str:
        self.submits += 1
        assert api_key == b"asr-secret"
        assert "sig=read" in request.signed_url
        return request.request_id

    async def poll(self, *, api_key: bytes, task_id: str) -> VolcanoPollResult:
        self.polls += 1
        return VolcanoPollResult(
            completed=True,
            payload={
                "result": {
                    "utterances": [
                        {
                            "start_time": 0,
                            "end_time": 1000,
                            "text": "测试完成。",
                        }
                    ]
                }
            },
        )


async def seeded(tmp_path):
    runtime_root = tmp_path / "runtime"
    source = runtime_root / "staging" / "job" / "file.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original-audio")
    source_sha256 = hashlib.sha256(b"original-audio").hexdigest()
    database = Database(tmp_path / "pipeline.sqlite3")
    await database.create_schema()
    job_id, file_id = str(uuid4()), str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage="transcribing"))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="hidden.mp3",
                extension=".mp3",
                mime_type="audio/mpeg",
                size_bytes=len(b"original-audio"),
                sha256=source_sha256,
                duration_ms=2000,
                position=0,
                temporary_path=str(source),
            )
        )
        await session.commit()
    repository = AsrRepository(database)
    task = await repository.ensure_file_task(
        job_id=job_id,
        job_file_id=file_id,
        relative_source_path="staging/job/file.mp3",
        sha256=source_sha256,
    )
    return database, runtime_root, repository, task


@pytest.mark.asyncio
async def test_full_file_pipeline_is_idempotent_and_deletes_storage(tmp_path) -> None:
    database, runtime_root, repository, task = await seeded(tmp_path)
    storage, volcano = Storage(), Volcano()
    coordinator = VolcanoAsrCoordinator(
        database=database,
        runtime_root=runtime_root,
        repository=repository,
        storage=storage,
        volcano=volcano,
        keychain=Keychain(),
    )

    assert await coordinator.advance_task(task.id) == "completed"
    assert await coordinator.advance_task(task.id) == "completed"

    assert (storage.created, storage.uploaded, storage.read_urls, storage.deleted) == (
        1,
        1,
        1,
        1,
    )
    assert (volcano.submits, volcano.polls) == (1, 1)
    async with database.session() as session:
        transcripts = list(await session.scalars(select(Transcript)))
    assert [item.text for item in transcripts] == ["测试完成。"]
    await database.dispose()


@pytest.mark.asyncio
async def test_existing_remote_task_is_polled_without_upload_or_resubmit(tmp_path) -> None:
    database, runtime_root, repository, task = await seeded(tmp_path)
    await repository.mark_storage_uploaded(task.id, "obj_random")
    await repository.mark_submitted(task.id, task.request_id)
    storage, volcano = Storage(), Volcano()
    coordinator = VolcanoAsrCoordinator(
        database=database,
        runtime_root=runtime_root,
        repository=repository,
        storage=storage,
        volcano=volcano,
        keychain=Keychain(),
    )

    assert await coordinator.advance_task(task.id) == "completed"

    assert (storage.created, storage.uploaded, storage.read_urls) == (0, 0, 0)
    assert (volcano.submits, volcano.polls) == (0, 1)
    await database.dispose()
