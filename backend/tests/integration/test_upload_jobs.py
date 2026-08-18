from __future__ import annotations

import subprocess
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

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
from audio_memory.models import AnalysisJob, JobFile, Transcript
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.prompts.store import PromptStore


class RetryCoordinator:
    async def snapshot_active_with_generation(self):
        return (
            SimpleNamespace(provider_id="deepseek", model_id="deepseek-v4-flash"),
            8,
        )

    async def validate_saved(self, provider_id):
        return SimpleNamespace(ok=True)


class RetryTaskCoordinator:
    def __init__(self):
        self.called = asyncio.Event()
        self.analysis_request = None
        self.method = None

    async def submit_new_upload(self, analysis_request):
        self.method = "new"
        self.analysis_request = analysis_request
        self.called.set()

    async def retry_failed_upload_in_place(
        self, *, source_job_id, provider_id, model_id, credential_generation
    ):
        self.method = "resume"
        self.analysis_request = SimpleNamespace(
            source_job_id=source_job_id,
            provider_id=provider_id,
            model_id=model_id,
            credential_generation=credential_generation,
            priority=0,
        )
        self.called.set()
        return SimpleNamespace(id="resumed-version")


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
    app.state.eta_tracker = TranscriptionEtaTracker()
    app.state.upload_service = UploadService(
        database, paths, eta_tracker=app.state.eta_tracker
    )
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
@pytest.mark.parametrize(
    "error_code",
    [
        "model_analysis_failed",
        "credential_changed",
        "fixed_rules_changed",
        "network_timeout",
        "authentication_failed",
        "insufficient_balance",
        "rate_limited",
        "provider_unavailable",
        "content_rejected",
        "model_response_invalid",
        "model_output_truncated",
        "report_audit_pending",
        "event_map_schema_invalid",
        "event_map_unknown_segment",
        "event_map_coverage_invalid",
        "analysis_quality_insufficient",
        "autonomous_day_map_invalid",
        "autonomous_search_decision_invalid",
    ],
)
async def test_failed_model_analysis_retries_with_active_provider_without_whisper(
    job_client, error_code
):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.FAILED.value
        job.error_code = error_code
        await session.commit()
    task_coordinator = RetryTaskCoordinator()
    prompt_store = PromptStore(client._transport.app.state.upload_service.paths.prompts)
    prompt_store.initialize()
    client._transport.app.state.provider_coordinator = RetryCoordinator()
    client._transport.app.state.analysis_task_coordinator = task_coordinator
    client._transport.app.state.prompt_store = prompt_store
    client._transport.app.state.database = database
    client._transport.app.state.transcription_tasks = {}

    response = await client.post(f"/api/jobs/{job_id}/retry-analysis")
    await asyncio.wait_for(task_coordinator.called.wait(), timeout=1)

    assert response.status_code == 202
    assert task_coordinator.analysis_request.provider_id == "deepseek"
    assert task_coordinator.analysis_request.model_id == "deepseek-v4-flash"
    assert task_coordinator.analysis_request.credential_generation == 8
    assert task_coordinator.analysis_request.priority == 0
    assert task_coordinator.method == "resume"


@pytest.mark.asyncio
async def test_active_job_endpoint_returns_latest_recoverable_job(job_client):
    client, _, database = job_client
    older_id = (await client.post("/api/jobs")).json()["id"]
    latest_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        older = await session.get(AnalysisJob, older_id)
        older.stage = JobStage.COMPLETED.value
        latest = await session.get(AnalysisJob, latest_id)
        latest.stage = JobStage.INTERRUPTED.value
        latest.provider_id = "deepseek"
        latest.model_id = "deepseek-chat"
        await session.commit()

    response = await client.get("/api/jobs/active")

    assert response.status_code == 200
    assert response.json()["id"] == latest_id
    assert response.json()["stage"] == JobStage.INTERRUPTED.value


@pytest.mark.asyncio
async def test_job_view_reports_real_transcription_progress(job_client):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    file_id = str(uuid4())
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.TRANSCRIBING.value
        session.add(JobFile(
            id=file_id, job_id=job_id, original_name="long.mp3", extension=".mp3",
            size_bytes=100, sha256="b" * 64, duration_ms=1_000_000, position=0,
            temporary_path="/tmp/long.mp3",
        ))
        session.add(Transcript(
            id=str(uuid4()), job_file_id=file_id, segment_index=0,
            start_ms=0, end_ms=250_000, text="已完成四分之一", words_json="[]",
        ))
        await session.commit()

    response = await client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["progress_percent"] == 25
    assert response.json()["eta_state"] == "estimating"
    assert response.json()["eta_seconds"] is None


@pytest.mark.asyncio
async def test_job_view_reports_dynamic_transcription_eta(job_client):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    file_id = str(uuid4())
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.TRANSCRIBING.value
        session.add(JobFile(
            id=file_id, job_id=job_id, original_name="long.mp3", extension=".mp3",
            size_bytes=100, sha256="c" * 64, duration_ms=1_000_000, position=0,
            temporary_path="/tmp/long.mp3",
        ))
        session.add(Transcript(
            id=str(uuid4()), job_file_id=file_id, segment_index=0,
            start_ms=0, end_ms=300_000, text="第一段", words_json="[]",
        ))
        await session.commit()
    client._transport.app.state.eta_tracker.record(job_id, 300_000, 30)

    response = await client.get(f"/api/jobs/{job_id}")

    assert response.json()["eta_state"] == "ready"
    assert response.json()["eta_seconds"] == 70
