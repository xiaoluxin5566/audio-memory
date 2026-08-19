from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.analysis.task_coordinator import AnalysisRequest, AnalysisTaskCoordinator
from audio_memory.api.jobs import run_pipeline
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript


@pytest.mark.asyncio
async def test_transcription_handoff_reaches_fake_provider_and_completion(
    tmp_path: Path, caplog
) -> None:
    database = Database(tmp_path / "analysis-handoff.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    transcript_id = str(uuid4())
    async with database.session() as session:
        session.add(
            AnalysisJob(
                id=job_id,
                stage="transcribing",
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
            )
        )
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="fixture.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="a" * 64,
                duration_ms=1_000,
                position=0,
                temporary_path=str(tmp_path / "fixture.mp3"),
            )
        )
        session.add(
            Transcript(
                id=transcript_id,
                job_file_id=file_id,
                segment_index=0,
                start_ms=0,
                end_ms=1_000,
                text="locally preserved transcript",
                words_json="[]",
            )
        )
        await session.commit()

    provider = object.__new__(ProviderAnalysisClient)
    provider._remote_lock = asyncio.Lock()
    provider._parallel_audit_limit = asyncio.Semaphore(1)
    provider._generate_serialized = AsyncMock(return_value="fake report")
    completed = asyncio.Event()
    released: list[str] = []
    released_event = asyncio.Event()

    async def release(released_job_id: str) -> None:
        released.append(released_job_id)
        released_event.set()

    class FakePublishingRunner:
        async def run(self, version_id: str, _owner_id: str) -> None:
            report = await provider.generate_markdown(
                "deepseek",
                model_id="deepseek-v4-pro",
                system="fake system",
                user="fake user",
            )
            assert report == "fake report"
            async with database.session() as session:
                version = await session.get(AnalysisVersion, version_id)
                job = await session.get(AnalysisJob, job_id)
                assert version is not None and job is not None
                version.status = "completed"
                job.stage = "completed"
                await session.commit()
            completed.set()

    coordinator = AnalysisTaskCoordinator(database, on_upload_finished=release)
    await coordinator.start(FakePublishingRunner())

    class CompletedTranscription:
        async def run_job(self, _job_id, _engine):
            return None

    class SleepPrevention:
        async def release(self, released_job_id):
            await release(released_job_id)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                transcription_service=CompletedTranscription(),
                whisper_engine=object(),
                analysis_task_coordinator=coordinator,
                database=database,
                sleep_prevention=SleepPrevention(),
            )
        )
    )
    analysis_request = AnalysisRequest(
        source_job_id=job_id,
        source_batch_id=None,
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        credential_generation=1,
        prompt_snapshot={},
        profile_snapshot=[],
        priority=0,
    )
    caplog.set_level("INFO", logger="uvicorn.error")

    await run_pipeline(
        request,
        job_id,
        analysis_request,
        sleep_protected=True,
    )
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.wait_for(released_event.wait(), timeout=1)

    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        version = await session.scalar(
            select(AnalysisVersion).where(AnalysisVersion.source_job_id == job_id)
        )
        transcript = await session.get(Transcript, transcript_id)
    assert job is not None and job.stage == "completed"
    assert version is not None and version.status == "completed"
    assert transcript is not None and transcript.text == "locally preserved transcript"
    assert released == [job_id]
    provider._generate_serialized.assert_awaited_once()

    events = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.message)["event"])
        except (TypeError, KeyError, json.JSONDecodeError):
            continue
    assert events == [
        "transcription.completed",
        "analysis.enqueue.started",
        "analysis.enqueue.lock_acquired",
        "analysis.enqueue.transaction_started",
        "analysis.enqueue.committed",
        "analysis.enqueue.worker_notified",
        "analysis.worker.claimed",
        "analysis.provider.request_started",
        "analysis.provider.request_finished",
    ]
    assert all("locally preserved transcript" not in record.message for record in caplog.records)

    await coordinator.close()
    await database.dispose()
