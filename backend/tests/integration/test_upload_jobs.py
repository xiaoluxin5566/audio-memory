from __future__ import annotations

import subprocess
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from audio_memory.api.jobs import router
from audio_memory.config import AppPaths
from audio_memory.db import Database
from audio_memory.uploads.service import UploadService
from audio_memory.uploads.cleanup import cleanup_abandoned_uploads
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob


class RetryCoordinator:
    async def snapshot_active(self):
        return SimpleNamespace(provider_id="deepseek", model_id="deepseek-v4-flash")

    async def validate_saved(self, provider_id):
        return SimpleNamespace(ok=True)


class RetryOrchestrator:
    def __init__(self):
        self.called = asyncio.Event()
        self.provider_snapshot = None

    async def run(self, job_id, provider_snapshot):
        self.provider_snapshot = provider_snapshot
        self.called.set()


def make_audio(path: Path, codec: str) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.1",
            "-c:a",
            codec,
            "-y",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@pytest_asyncio.fixture
async def job_client(tmp_path: Path):
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    app = FastAPI()
    app.state.upload_service = UploadService(database, paths)
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, paths, database
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "codec", "allowed"),
    [("a.mp3", "libmp3lame", True), ("b.aac", "aac", True)],
)
async def test_valid_audio_upload_contract(job_client, tmp_path, filename, codec, allowed):
    client, _, _ = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    content = make_audio(tmp_path / filename, codec)

    response = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": (filename, content, "audio/mpeg")},
    )

    assert (response.status_code == 201) is allowed
    assert response.json()["size_bytes"] == len(content)
    assert response.json()["upload_progress"] == 100


@pytest.mark.asyncio
async def test_extension_and_content_must_both_be_supported(job_client):
    client, _, _ = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]

    response = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("fake.mp3", b"not audio", "audio/mpeg")},
    )

    assert response.status_code == 415
    assert response.json()["detail"]["message"] == (
        "不支持该文件格式，请上传 MP3/AAC 格式文件"
    )
    assert response.json()["detail"]["file_id"]


@pytest.mark.asyncio
async def test_invalid_file_pauses_batch_until_removed(job_client, tmp_path):
    client, _, _ = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    rejected = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("bad.wav", b"RIFF-invalid", "audio/wav")},
    )
    rejected_id = rejected.json()["detail"]["file_id"]
    valid = make_audio(tmp_path / "after.mp3", "libmp3lame")

    paused = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("after.mp3", valid, "audio/mpeg")},
    )
    removed = await client.delete(
        f"/api/jobs/{job_id}/files/{rejected_id}"
    )
    resumed = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("after.mp3", valid, "audio/mpeg")},
    )

    assert paused.status_code == 409
    assert removed.status_code == 204
    assert resumed.status_code == 201


@pytest.mark.asyncio
async def test_duplicate_audio_is_rejected(job_client, tmp_path):
    client, _, _ = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    content = make_audio(tmp_path / "same.mp3", "libmp3lame")

    first = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("one.mp3", content, "audio/mpeg")},
    )
    duplicate = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("two.mp3", content, "audio/mpeg")},
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_abandoned_upload_is_cleaned_on_next_start(job_client, tmp_path):
    client, paths, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    content = make_audio(tmp_path / "abandoned.mp3", "libmp3lame")
    uploaded = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("abandoned.mp3", content, "audio/mpeg")},
    )
    staged_path = paths.staging / job_id / f"{uploaded.json()['id']}.mp3"

    cleaned = await cleanup_abandoned_uploads(database, paths.staging)
    missing = await client.get(f"/api/jobs/{job_id}")

    assert cleaned == 1
    assert staged_path.exists() is False
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_failed_model_analysis_retries_with_active_provider_without_whisper(job_client):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.FAILED.value
        job.error_code = "model_analysis_failed"
        await session.commit()
    orchestrator = RetryOrchestrator()
    client._transport.app.state.provider_coordinator = RetryCoordinator()
    client._transport.app.state.analysis_orchestrator = orchestrator
    client._transport.app.state.transcription_tasks = {}

    response = await client.post(f"/api/jobs/{job_id}/retry-analysis")
    await asyncio.wait_for(orchestrator.called.wait(), timeout=1)

    assert response.status_code == 202
    assert orchestrator.provider_snapshot == {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-flash",
    }
