from __future__ import annotations

import subprocess
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

import audio_memory.uploads.service as upload_service_module
from audio_memory.api.jobs import router
from audio_memory.config import AppPaths, PinnedDevelopmentRoot, RuntimeConfig
from audio_memory.db import Database
from audio_memory.uploads.service import UploadError, UploadService
from audio_memory.uploads.cleanup import (
    UnsafeCleanupPathError,
    cleanup_abandoned_uploads,
    move_staged_directory,
    remove_staged_entry,
)
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, AnalysisVersion, Batch, JobFile, TempFileManifest, Transcript
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.prompts.store import PromptStore
from audio_memory.prompts.composer import PromptComposer
from audio_memory.power.sleep_prevention import SleepPreventionManager
from audio_memory.repositories import AppSettingsRepository
from audio_memory.analysis.task_coordinator import AlreadyRunningError


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


class AlreadyRunningRetryTaskCoordinator(RetryTaskCoordinator):
    async def retry_failed_upload_in_place(self, **_kwargs):
        return None

    async def submit_new_upload(self, _analysis_request):
        raise AlreadyRunningError("Analysis is already pending or running")


class CancelTaskCoordinator:
    async def cancel_new_upload(self, _job_id):
        return False


def test_development_boundary_removes_whisper_chunk_directory(tmp_path):
    data_root = tmp_path / "project/.runtime/dev"
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={
            "AUDIO_MEMORY_PROFILE": "development",
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_MODEL_ROOT": str(data_root / "models"),
        },
    )
    boundary = PinnedDevelopmentRoot.open(config, create=True)
    assert boundary is not None
    try:
        boundary.ensure_directories()
        chunks = config.paths.staging / "job-1/file-1.whisper-chunks"
        boundary.create_directory(chunks.parent)
        boundary.create_directory(chunks)
        boundary.write_text_atomic(chunks / "checkpoint.json", "{}")

        remove_staged_entry(
            chunks,
            config.paths.staging,
            write_boundary=boundary,
        )

        assert not chunks.exists()
    finally:
        boundary.close()


def test_production_staging_move_refuses_symlinked_job_directory(tmp_path):
    staging = tmp_path / "staging"
    other_job = staging / "job-b"
    other_job.mkdir(parents=True)
    protected = other_job / "keep.mp3"
    protected.write_bytes(b"preserve")
    linked_job = staging / "job-a"
    linked_job.symlink_to(other_job, target_is_directory=True)
    quarantine = staging / ".cancelled-job-a"

    with pytest.raises(UnsafeCleanupPathError):
        move_staged_directory(linked_job, quarantine, staging)

    assert linked_job.is_symlink()
    assert protected.read_bytes() == b"preserve"
    assert not quarantine.exists()


def test_production_staging_cleanup_refuses_symlinked_quarantine(tmp_path):
    staging = tmp_path / "staging"
    other_job = staging / "job-b"
    other_job.mkdir(parents=True)
    protected = other_job / "keep.mp3"
    protected.write_bytes(b"preserve")
    quarantine = staging / ".cancelled-job-a"
    quarantine.symlink_to(other_job, target_is_directory=True)

    with pytest.raises(UnsafeCleanupPathError):
        remove_staged_entry(quarantine, staging)

    assert quarantine.is_symlink()
    assert protected.read_bytes() == b"preserve"


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
    app.state.settings_repository = AppSettingsRepository(database)
    app.state.sleep_prevention = SleepPreventionManager(platform_name="Test")
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
@pytest.mark.parametrize("version_status", ["pending", "running"])
async def test_job_api_projects_analysis_phase_from_durable_version(
    job_client, version_status
):
    client, paths, database = job_client
    job_id = str(uuid4())
    async with database.session() as session:
        session.add(
            AnalysisJob(
                id=job_id,
                stage=JobStage.ANALYZING.value,
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
            )
        )
        session.add(
            AnalysisVersion(
                id=str(uuid4()),
                source_job_id=job_id,
                batch_id=None,
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="f" * 64,
                staged_results_json="{}",
                status=version_status,
                worker_owner_id="worker-1" if version_status == "running" else None,
                lease_expires_at=(
                    "2099-01-01T00:00:00+00:00"
                    if version_status == "running"
                    else None
                ),
            )
        )
        await session.commit()

    response = await client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["analysis_phase"] == version_status


@pytest.mark.asyncio
async def test_job_api_projects_safe_durable_analysis_detail_phase(job_client):
    client, _, database = job_client
    job_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.ANALYZING.value))
        session.add(AnalysisVersion(
            id=str(uuid4()), source_job_id=job_id, batch_id=None,
            provider_id="deepseek", model_id="deepseek-v4-pro",
            credential_generation=1, prompt_snapshot_json="{}",
            profile_snapshot_json="[]", fixed_rules_hash="f" * 64,
            staged_results_json="{}", status="running",
            worker_owner_id="worker-1",
            lease_expires_at="2099-01-01T00:00:00+00:00",
            pipeline_checkpoints_json='{"report_phase":"auditing"}',
        ))
        await session.commit()

    response = await client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["analysis_detail_phase"] == "auditing"


@pytest.mark.asyncio
async def test_job_api_never_claims_model_running_without_a_version(job_client):
    client, _, database = job_client
    job_id = str(uuid4())
    async with database.session() as session:
        session.add(
            AnalysisJob(
                id=job_id,
                stage=JobStage.ANALYZING.value,
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
            )
        )
        await session.commit()

    response = await client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["analysis_phase"] == "failed"


@pytest.mark.asyncio
async def test_missing_audio_runtime_is_not_reported_as_unsupported_format(
    job_client, monkeypatch
):
    client, _, _ = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    monkeypatch.setenv("AUDIO_MEMORY_RELEASE_MODE", "1")
    monkeypatch.setenv("AUDIO_MEMORY_RELEASE_ROOT", "/missing-audio-memory-release")
    monkeypatch.delenv("AUDIO_MEMORY_FFPROBE", raising=False)

    response = await client.post(
        f"/api/jobs/{job_id}/files",
        files={"file": ("real-name.mp3", b"audio bytes", "audio/mpeg")},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "audio_runtime_unavailable"
    assert "音频组件" in response.json()["detail"]["message"]


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
@pytest.mark.parametrize(
    "stage",
    [
        JobStage.TRANSCRIBING.value,
        JobStage.ANALYZING.value,
        JobStage.READY_TO_COMMIT.value,
        JobStage.INTERRUPTED.value,
        JobStage.FAILED.value,
    ],
)
async def test_non_uploading_job_file_cannot_be_deleted_through_api(
    job_client, stage
):
    client, paths, database = job_client
    job_id = str(uuid4())
    file_id = str(uuid4())
    staged_path = paths.staging / job_id / f"{file_id}.mp3"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b"processing audio")
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=stage))
        session.add(JobFile(
            id=file_id,
            job_id=job_id,
            original_name="processing.mp3",
            extension=".mp3",
            mime_type="audio/mpeg",
            size_bytes=16,
            sha256="a" * 64,
            duration_ms=1_000,
            position=0,
            temporary_path=str(staged_path),
        ))
        await session.commit()

    response = await client.delete(f"/api/jobs/{job_id}/files/{file_id}")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "file_locked_during_processing",
        "message": "任务进行中，不能删除音频文件",
        "stage": stage,
    }
    async with database.session() as session:
        assert await session.get(JobFile, file_id) is not None
    assert staged_path.exists()


@pytest.mark.asyncio
async def test_cancel_current_job_removes_chunk_directories_and_preserves_history(
    job_client,
):
    client, paths, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    file_id = str(uuid4())
    source = paths.staging / job_id / f"{file_id}.mp3"
    chunks = paths.staging / job_id / f"{file_id}.whisper-chunks"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"interrupted audio")
    chunks.mkdir()
    (chunks / "checkpoint.json").write_text("{}")
    historical_job_id = str(uuid4())
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.INTERRUPTED.value
        session.add(AnalysisJob(
            id=historical_job_id,
            stage=JobStage.COMPLETED.value,
        ))
        session.add(JobFile(
            id=file_id, job_id=job_id, original_name="interrupted.mp3",
            extension=".mp3", size_bytes=17, sha256="a" * 64,
            duration_ms=1_000, position=0, temporary_path=str(source),
        ))
        session.add(TempFileManifest(
            id=str(uuid4()), task_uuid=job_id, file_path=str(chunks),
        ))
        await session.commit()

    client._transport.app.state.transcription_tasks = {}
    client._transport.app.state.analysis_task_coordinator = CancelTaskCoordinator()

    response = await client.delete(f"/api/jobs/{job_id}")

    assert response.status_code == 204
    async with database.session() as session:
        assert await session.get(AnalysisJob, job_id) is None
        assert await session.get(AnalysisJob, historical_job_id) is not None
    assert not source.exists()
    assert not chunks.exists()


@pytest.mark.asyncio
async def test_cancel_current_job_finishes_without_touching_unsafe_manifest_path(
    job_client,
):
    client, paths, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    file_id = str(uuid4())
    source = paths.staging / job_id / f"{file_id}.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"interrupted audio")
    protected = paths.root.parent / "must-not-delete.txt"
    protected.write_text("preserve")
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.INTERRUPTED.value
        session.add(JobFile(
            id=file_id, job_id=job_id, original_name="interrupted.mp3",
            extension=".mp3", size_bytes=17, sha256="c" * 64,
            duration_ms=1_000, position=0, temporary_path=str(source),
        ))
        session.add(TempFileManifest(
            id=str(uuid4()), task_uuid=job_id, file_path=str(protected),
        ))
        await session.commit()

    await client._transport.app.state.upload_service.cancel_job(job_id)

    async with database.session() as session:
        assert await session.get(AnalysisJob, job_id) is None
    assert protected.read_text() == "preserve"


@pytest.mark.asyncio
async def test_cancel_cleanup_failure_is_persisted_and_retried(
    job_client, monkeypatch
):
    client, paths, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    file_id = str(uuid4())
    source = paths.staging / job_id / f"{file_id}.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"interrupted audio")
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.INTERRUPTED.value
        session.add(JobFile(
            id=file_id, job_id=job_id, original_name="interrupted.mp3",
            extension=".mp3", size_bytes=17, sha256="e" * 64,
            duration_ms=1_000, position=0, temporary_path=str(source),
        ))
        await session.commit()

    def fail_final_cleanup(*_args, **_kwargs):
        raise PermissionError("simulated cleanup failure")

    monkeypatch.setattr(
        upload_service_module, "remove_staged_entry", fail_final_cleanup
    )
    await client._transport.app.state.upload_service.cancel_job(job_id)

    async with database.session() as session:
        assert await session.get(AnalysisJob, job_id) is None
        pending = await session.scalar(
            select(TempFileManifest).where(
                TempFileManifest.cleanup_status == "cancelled_pending"
            )
        )
        assert pending is not None
        quarantine = Path(pending.file_path)
    assert quarantine.exists()

    cleaned = await cleanup_abandoned_uploads(database, paths.staging)

    assert cleaned == 1
    assert not quarantine.exists()
    async with database.session() as session:
        assert await session.get(TempFileManifest, pending.id) is None


@pytest.mark.asyncio
async def test_resume_rejects_missing_source_audio_before_claiming_success(job_client):
    client, paths, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    missing_source = paths.staging / job_id / "missing.mp3"
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.INTERRUPTED.value
        job.provider_id = "deepseek"
        job.model_id = "deepseek-chat"
        session.add(JobFile(
            id=str(uuid4()), job_id=job_id, original_name="missing.mp3",
            extension=".mp3", size_bytes=10, sha256="b" * 64,
            duration_ms=1_000, position=0, temporary_path=str(missing_source),
        ))
        await session.commit()
    client._transport.app.state.transcription_tasks = {}

    response = await client.post(f"/api/jobs/{job_id}/resume")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "resume_source_missing",
        "message": "原始音频文件已缺失，请取消任务后重新上传",
    }


@pytest.mark.asyncio
async def test_resume_rejects_truncated_source_audio_before_claiming_success(
    job_client,
):
    client, paths, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    source = paths.staging / job_id / "truncated.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"short")
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.INTERRUPTED.value
        job.provider_id = "deepseek"
        job.model_id = "deepseek-chat"
        session.add(JobFile(
            id=str(uuid4()), job_id=job_id, original_name="truncated.mp3",
            extension=".mp3", size_bytes=10, sha256="d" * 64,
            duration_ms=1_000, position=0, temporary_path=str(source),
        ))
        await session.commit()
    client._transport.app.state.transcription_tasks = {}

    response = await client.post(f"/api/jobs/{job_id}/resume")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "resume_source_missing"


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
async def test_duplicate_analysis_retry_returns_current_running_state(job_client, caplog):
    client, _, database = job_client
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.FAILED.value
        job.error_code = "model_output_truncated"
        await session.commit()
    client._transport.app.state.provider_coordinator = RetryCoordinator()
    client._transport.app.state.analysis_task_coordinator = (
        AlreadyRunningRetryTaskCoordinator()
    )
    prompt_store = PromptStore(client._transport.app.state.upload_service.paths.prompts)
    prompt_store.initialize()
    client._transport.app.state.prompt_store = prompt_store
    client._transport.app.state.database = database
    client._transport.app.state.transcription_tasks = {}

    response = await client.post(f"/api/jobs/{job_id}/retry-analysis")

    assert response.status_code == 202
    assert response.json()["stage"] == JobStage.ANALYZING.value
    assert response.json()["already_running"] is True
    events = [json.loads(record.message) for record in caplog.records if record.message.startswith("{")]
    duplicate = [item for item in events if item["event"] == "analysis.retry.duplicate_accepted"]
    assert duplicate[-1]["job_id"] == job_id
    assert duplicate[-1]["retry_path"] == "new_upload_submission"


@pytest.mark.asyncio
async def test_legacy_completed_unaudited_report_can_continue_audit(job_client):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.COMPLETED.value
        session.add(Batch(id="legacy-published-batch", job_id=job_id, natural_date="2026-08-18"))
        await session.flush()
        session.add(
            AnalysisVersion(
                id="legacy-unaudited-version",
                source_job_id=job_id,
                batch_id="legacy-published-batch",
                provider_id="deepseek",
                model_id="deepseek-v4-flash",
                credential_generation=8,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash=PromptComposer.fixed_rules_hash(),
                staged_results_json='{"direct_report_v1_markdown":"# Existing V1","direct_report_publication_metadata":{"audit_status":"completed_unaudited"}}',
                status="completed",
            )
        )
        await session.commit()
    task_coordinator = RetryTaskCoordinator()
    client._transport.app.state.provider_coordinator = RetryCoordinator()
    client._transport.app.state.analysis_task_coordinator = task_coordinator
    client._transport.app.state.transcription_tasks = {}

    response = await client.post(f"/api/jobs/{job_id}/retry-analysis")

    assert response.status_code == 202
    assert task_coordinator.method == "resume"


@pytest.mark.asyncio
async def test_interrupted_job_with_failed_analysis_can_continue_audit(job_client):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.INTERRUPTED.value
        session.add(
            AnalysisVersion(
                id="interrupted-failed-version",
                source_job_id=job_id,
                batch_id=None,
                provider_id="deepseek",
                model_id="deepseek-v4-flash",
                credential_generation=8,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash=PromptComposer.fixed_rules_hash(),
                staged_results_json='{"direct_report_v1_markdown":"# Existing V1"}',
                status="failed",
                error_code="model_analysis_failed",
            )
        )
        await session.commit()
    task_coordinator = RetryTaskCoordinator()
    client._transport.app.state.provider_coordinator = RetryCoordinator()
    client._transport.app.state.analysis_task_coordinator = task_coordinator
    client._transport.app.state.transcription_tasks = {}

    response = await client.post(f"/api/jobs/{job_id}/retry-analysis")

    assert response.status_code == 202
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
async def test_active_job_endpoint_keeps_the_prepublication_stage_locked(job_client):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.READY_TO_COMMIT.value
        await session.commit()

    response = await client.get("/api/jobs/active")

    assert response.status_code == 200
    assert response.json()["id"] == job_id
    assert response.json()["stage"] == JobStage.READY_TO_COMMIT.value
    assert response.json()["analysis_phase"] == "running"
    assert response.json()["analysis_detail_phase"] == "publishing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        JobStage.TRANSCRIBING.value,
        JobStage.ANALYZING.value,
        JobStage.READY_TO_COMMIT.value,
        JobStage.INTERRUPTED.value,
        JobStage.FAILED.value,
    ],
)
async def test_create_job_is_blocked_until_the_current_job_publishes_or_is_cleared(
    job_client, stage
):
    client, _, database = job_client
    active_job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        active_job = await session.get(AnalysisJob, active_job_id)
        active_job.stage = stage
        await session.commit()

    response = await client.post("/api/jobs")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "active_job_locked",
        "message": "当前任务尚未生成报告，请先完成、重试或清除当前任务",
        "job_id": active_job_id,
        "stage": stage,
    }


@pytest.mark.asyncio
async def test_create_job_is_allowed_after_the_current_job_publishes(job_client):
    client, _, database = job_client
    completed_job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        completed_job = await session.get(AnalysisJob, completed_job_id)
        completed_job.stage = JobStage.COMPLETED.value
        await session.commit()

    response = await client.post("/api/jobs")

    assert response.status_code == 201
    assert response.json()["id"] != completed_job_id


@pytest.mark.asyncio
async def test_precreated_upload_job_cannot_bypass_an_active_transcription(
    job_client,
):
    client, _, database = job_client
    active_job_id = (await client.post("/api/jobs")).json()["id"]
    waiting_job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        active_job = await session.get(AnalysisJob, active_job_id)
        active_job.stage = JobStage.TRANSCRIBING.value
        await session.commit()

    response = await client.post(
        f"/api/jobs/{waiting_job_id}/files",
        files={"file": ("later.mp3", b"not-read", "audio/mpeg")},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_job_locked"


@pytest.mark.asyncio
async def test_precreated_job_cannot_start_beside_an_active_transcription(
    job_client,
):
    client, _, database = job_client
    first_job_id = (await client.post("/api/jobs")).json()["id"]
    second_job_id = (await client.post("/api/jobs")).json()["id"]
    async with database.session() as session:
        for position, job_id in enumerate((first_job_id, second_job_id)):
            session.add(JobFile(
                id=str(uuid4()), job_id=job_id, original_name=f"{position}.mp3",
                extension=".mp3", size_bytes=10, sha256=str(position) * 64,
                duration_ms=1_000, position=0, temporary_path=f"/tmp/{position}.mp3",
            ))
        await session.commit()
    service = client._transport.app.state.upload_service
    await service.start(first_job_id, provider_id="deepseek", model_id="deepseek-chat")

    with pytest.raises(UploadError) as captured:
        await service.start(
            second_job_id, provider_id="deepseek", model_id="deepseek-chat"
        )

    assert captured.value.code == "active_job_locked"


@pytest.mark.asyncio
async def test_concurrent_precreated_jobs_cannot_both_enter_transcription(
    job_client,
):
    client, _, database = job_client
    job_ids = [(await client.post("/api/jobs")).json()["id"] for _ in range(2)]
    async with database.session() as session:
        for position, job_id in enumerate(job_ids):
            session.add(JobFile(
                id=str(uuid4()), job_id=job_id, original_name=f"{position}.mp3",
                extension=".mp3", size_bytes=10, sha256=str(position) * 64,
                duration_ms=1_000, position=0, temporary_path=f"/tmp/{position}.mp3",
            ))
        await session.commit()
    service = client._transport.app.state.upload_service

    results = await asyncio.gather(
        *(service.start(job_id, provider_id="deepseek", model_id="deepseek-chat")
          for job_id in job_ids),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    errors = [result for result in results if isinstance(result, UploadError)]
    assert len(errors) == 1
    assert errors[0].code == "active_job_locked"


@pytest.mark.asyncio
async def test_concurrent_start_and_delete_have_one_serialized_winner(job_client):
    client, paths, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    file_id = str(uuid4())
    staged_path = paths.staging / job_id / f"{file_id}.mp3"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b"processing audio")
    async with database.session() as session:
        session.add(JobFile(
            id=file_id, job_id=job_id, original_name="processing.mp3",
            extension=".mp3", size_bytes=16, sha256="a" * 64,
            duration_ms=1_000, position=0, temporary_path=str(staged_path),
        ))
        await session.commit()
    service = client._transport.app.state.upload_service

    started, removed = await asyncio.gather(
        service.start(job_id, provider_id="deepseek", model_id="deepseek-chat"),
        service.remove_file(job_id, file_id),
        return_exceptions=True,
    )

    outcomes = (started, removed)
    assert sum(not isinstance(result, BaseException) for result in outcomes) == 1
    errors = [result for result in outcomes if isinstance(result, UploadError)]
    assert len(errors) == 1
    assert errors[0].code in {"empty_batch", "file_locked_during_processing"}
    async with database.session() as session:
        persisted_file = await session.get(JobFile, file_id)
        persisted_job = await session.get(AnalysisJob, job_id)
    if errors[0].code == "file_locked_during_processing":
        assert persisted_job.stage == JobStage.TRANSCRIBING.value
        assert persisted_file is not None
        assert staged_path.exists()
    else:
        assert persisted_job.stage == JobStage.UPLOADING.value
        assert persisted_file is None
        assert not staged_path.exists()


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
async def test_job_view_reports_smooth_progress_bounded_by_current_batch(
    job_client,
):
    client, _, database = job_client
    job_id = (await client.post("/api/jobs")).json()["id"]
    first_file_id = str(uuid4())
    active_file_id = str(uuid4())
    now = [100.0]
    tracker = TranscriptionEtaTracker(clock=lambda: now[0])
    client._transport.app.state.eta_tracker = tracker
    client._transport.app.state.upload_service.eta_tracker = tracker
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        job.stage = JobStage.TRANSCRIBING.value
        session.add_all([
            JobFile(
                id=first_file_id, job_id=job_id, original_name="done.mp3",
                extension=".mp3", size_bytes=100, sha256="d" * 64,
                duration_ms=100_000, position=0, temporary_path="/tmp/done.mp3",
            ),
            JobFile(
                id=active_file_id, job_id=job_id, original_name="active.mp3",
                extension=".mp3", size_bytes=100, sha256="e" * 64,
                duration_ms=900_000, position=1, temporary_path="/tmp/active.mp3",
            ),
            Transcript(
                id=str(uuid4()), job_file_id=first_file_id, segment_index=0,
                start_ms=0, end_ms=100_000, text="已完成", words_json="[]",
            ),
        ])
        await session.commit()
    tracker.record(job_id, 100_000, 10)
    tracker.set_progress(
        job_id, "本地转写", current=2, total=4,
        file_id=active_file_id, unit_ms=100_000,
    )
    now[0] += 5

    response = await client.get(f"/api/jobs/{job_id}")

    assert response.json()["progress_percent"] == 10
    assert response.json()["live_progress_percent"] == 43.75


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
