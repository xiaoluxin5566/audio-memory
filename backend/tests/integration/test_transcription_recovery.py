from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import audio_memory.api.jobs as jobs_api
from audio_memory.db import Database
from audio_memory.api.jobs import run_pipeline, track_transcription
from audio_memory.diarization.alignment import AlignedTranscriptSegment, Word
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript
from audio_memory.transcription.checkpoints import TranscriptionService
from audio_memory.transcription.engine import MLXWhisperEngine, SelectiveRefiner
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.transcription.risk_service import TranscriptionRiskGateService
from audio_memory.transcription.segments import TranscriptSegment


class InterruptOnceEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.interrupted = False

    async def transcribe_file(self, file: JobFile, resume_from: int):
        self.calls.append((file.id, resume_from))
        for index in range(resume_from, 3):
            if index == 1 and not self.interrupted:
                self.interrupted = True
                raise RuntimeError("worker stopped")
            yield TranscriptSegment(
                file.id,
                index,
                index * 1000,
                (index + 1) * 1000,
                f"segment-{index}",
                [],
            )


@pytest.mark.asyncio
async def test_database_persistence_failure_never_logs_or_echoes_fast_transcript(
    tmp_path: Path, caplog
) -> None:
    # Formatting the SQLAlchemy exception at either orchestration boundary would
    # expose the bound fast-transcript parameter before it has passed the gate.
    secret = "FAST-TRANSCRIPT-SECRET-MUST-NEVER-LEAK"
    database = Database(tmp_path / "transcription-log-redaction.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="test.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="z" * 64,
                duration_ms=2_000,
                position=0,
                temporary_path=str(tmp_path / "test.mp3"),
            )
        )
        await session.commit()

    class DuplicateIndexEngine:
        async def transcribe_file(self, file: JobFile, _resume_from: int):
            for _ in range(2):
                yield TranscriptSegment(
                    file.id,
                    0,
                    0,
                    1_000,
                    secret,
                    [{"word": secret, "start_ms": 0, "end_ms": 1_000}],
                )

    service = TranscriptionService(
        database,
        risk_gate=TranscriptionRiskGateService(database),
        refiner=SelectiveRefiner(database),
    )
    state = SimpleNamespace(transcription_tasks={})
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    caplog.set_level(logging.DEBUG)
    logging.getLogger("audio_memory.transcription.checkpoints").disabled = False
    logging.getLogger("uvicorn.error").disabled = False
    track_transcription(request, job_id, service.run_job(job_id, DuplicateIndexEngine()))
    task = state.transcription_tasks[job_id]

    with pytest.raises(IntegrityError) as caught:
        await task
    await asyncio.sleep(0)

    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert "diagnostic=transcription_failed" in caplog.text
    assert "diagnostic=pipeline_failed" in caplog.text
    await database.dispose()


@pytest.mark.asyncio
async def test_available_model_probability_signals_are_persisted_for_calibration(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "transcript-probability-signals.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="test.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="y" * 64,
                duration_ms=1_000,
                position=0,
                temporary_path=str(tmp_path / "test.mp3"),
            )
        )
        await session.commit()

    await TranscriptionService(database)._save_segment(
        TranscriptSegment(
            file_id=file_id,
            index=0,
            start_ms=0,
            end_ms=1_000,
            text="probability calibration candidate",
            words=[],
            no_speech_prob=0.87,
            avg_logprob=-0.42,
        )
    )

    async with database.session() as session:
        stored = await session.scalar(
            select(Transcript).where(Transcript.job_file_id == file_id)
        )
    assert stored is not None
    assert stored.no_speech_prob == 0.87
    assert stored.avg_logprob == -0.42
    await database.dispose()


@pytest.mark.asyncio
async def test_interrupted_transcription_resumes_without_duplicates(tmp_path: Path) -> None:
    database = Database(tmp_path / "transcription.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="test.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="a" * 64,
                duration_ms=3000,
                position=0,
                temporary_path=str(tmp_path / "test.mp3"),
            )
        )
        await session.commit()
    service = TranscriptionService(
        database,
        risk_gate=TranscriptionRiskGateService(database),
        refiner=SelectiveRefiner(database),
    )
    engine = InterruptOnceEngine()

    with pytest.raises(RuntimeError):
        await service.run_job(job_id, engine)
    async with database.session() as session:
        interrupted = await session.get(AnalysisJob, job_id)
        assert interrupted.stage == JobStage.INTERRUPTED.value
        assert interrupted.error_code == "transcription_failed"
    await service.resume_job(job_id, engine)

    async with database.session() as session:
        rows = list(await session.scalars(Transcript.__table__.select()))
        job = await session.get(AnalysisJob, job_id)
    assert engine.calls == [(file_id, 0), (file_id, 1)]
    assert len(rows) == 3
    assert job.stage == JobStage.TRANSCRIBING.value
    await database.dispose()


@pytest.mark.asyncio
async def test_analysis_submission_failure_preserves_transcript_and_releases_sleep(
    tmp_path: Path, caplog
) -> None:
    database = Database(tmp_path / "analysis-submission-failure.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="preserved.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="p" * 64,
                duration_ms=1_000,
                position=0,
                temporary_path=str(tmp_path / "preserved.mp3"),
            )
        )
        session.add(
            Transcript(
                id=str(uuid4()),
                job_file_id=file_id,
                segment_index=0,
                start_ms=0,
                end_ms=1_000,
                text="must survive queue failure",
                words_json="[]",
            )
        )
        await session.commit()

    class CompletedTranscription:
        async def run_job(self, _job_id, _engine):
            return None

    class FailedSubmission:
        async def submit_new_upload(self, _analysis_request):
            raise RuntimeError("queue insertion failed")

    released: list[str] = []

    class SleepPrevention:
        async def release(self, released_job_id):
            released.append(released_job_id)

    state = SimpleNamespace(
        transcription_service=CompletedTranscription(),
        whisper_engine=object(),
        analysis_task_coordinator=FailedSubmission(),
        database=database,
        sleep_prevention=SleepPrevention(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    with pytest.raises(RuntimeError, match="queue insertion failed"):
        await run_pipeline(request, job_id, SimpleNamespace(), sleep_protected=True)

    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        transcript = await session.scalar(
            select(Transcript).where(Transcript.job_file_id == file_id)
        )
    assert job is not None
    assert job.stage == JobStage.FAILED.value
    assert job.error_code == "model_analysis_failed"
    assert transcript is not None
    assert transcript.text == "must survive queue failure"
    assert released == [job_id]
    structured = []
    for record in caplog.records:
        try:
            structured.append(json.loads(record.message))
        except (TypeError, json.JSONDecodeError):
            continue
    assert [item["event"] for item in structured] == [
        "transcription.completed",
        "analysis.job.failed",
    ]
    assert all("must survive queue failure" not in record.message for record in caplog.records)
    await database.dispose()


@pytest.mark.asyncio
async def test_analysis_submission_timeout_is_failed_and_releases_sleep(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(tmp_path / "analysis-submission-timeout.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        await session.commit()

    class CompletedTranscription:
        async def run_job(self, _job_id, _engine):
            return None

    class HangingSubmission:
        async def submit_new_upload(self, _analysis_request):
            await asyncio.Event().wait()

    released: list[str] = []

    class SleepPrevention:
        async def release(self, released_job_id):
            released.append(released_job_id)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                transcription_service=CompletedTranscription(),
                whisper_engine=object(),
                analysis_task_coordinator=HangingSubmission(),
                database=database,
                sleep_prevention=SleepPrevention(),
            )
        )
    )
    monkeypatch.setattr(jobs_api, "ANALYSIS_SUBMISSION_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(TimeoutError):
        await run_pipeline(request, job_id, SimpleNamespace(), sleep_protected=True)

    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
    assert job is not None
    assert (job.stage, job.error_code) == (
        JobStage.FAILED.value,
        "model_analysis_failed",
    )
    assert released == [job_id]
    await database.dispose()


@pytest.mark.asyncio
async def test_analysis_submission_cancellation_is_not_swallowed(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "analysis-submission-cancelled.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        await session.commit()
    started = asyncio.Event()

    class CompletedTranscription:
        async def run_job(self, _job_id, _engine):
            return None

    class HangingSubmission:
        async def submit_new_upload(self, _analysis_request):
            started.set()
            await asyncio.Event().wait()

    released: list[str] = []

    class SleepPrevention:
        async def release(self, released_job_id):
            released.append(released_job_id)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                transcription_service=CompletedTranscription(),
                whisper_engine=object(),
                analysis_task_coordinator=HangingSubmission(),
                database=database,
                sleep_prevention=SleepPrevention(),
            )
        )
    )
    pipeline = asyncio.create_task(
        run_pipeline(request, job_id, SimpleNamespace(), sleep_protected=True)
    )
    await started.wait()
    pipeline.cancel()
    result = await asyncio.gather(pipeline, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert released == [job_id]
    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
    assert job is not None
    assert (job.stage, job.error_code) == (
        JobStage.FAILED.value,
        "model_analysis_failed",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_error_after_commit_keeps_durable_queue_and_sleep_ownership(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "analysis-submission-committed.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        await session.commit()

    class CompletedTranscription:
        async def run_job(self, _job_id, _engine):
            return None

    class CommittedSubmission:
        async def submit_new_upload(self, _analysis_request):
            async with database.session() as session:
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
                        fixed_rules_hash="x" * 64,
                        staged_results_json="{}",
                        status="pending",
                    )
                )
                job = await session.get(AnalysisJob, job_id)
                assert job is not None
                job.stage = JobStage.ANALYZING.value
                await session.commit()
            raise TimeoutError("response lost after commit")

    released: list[str] = []

    class SleepPrevention:
        async def release(self, released_job_id):
            released.append(released_job_id)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                transcription_service=CompletedTranscription(),
                whisper_engine=object(),
                analysis_task_coordinator=CommittedSubmission(),
                database=database,
                sleep_prevention=SleepPrevention(),
            )
        )
    )

    await run_pipeline(request, job_id, SimpleNamespace(), sleep_protected=True)

    async with database.session() as session:
        job = await session.get(AnalysisJob, job_id)
        version = await session.scalar(
            select(AnalysisVersion).where(AnalysisVersion.source_job_id == job_id)
        )
    assert job is not None and job.stage == JobStage.ANALYZING.value
    assert version is not None and version.status == "pending"
    assert released == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["run", "resume"])
async def test_transcription_passes_bulk_wall_clock_and_never_runs_refinement(
    tmp_path: Path, entrypoint: str
) -> None:
    """The production and recovery paths must enforce the 20% refinement budget."""
    database = Database(tmp_path / f"risk-budget-{entrypoint}.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(
            AnalysisJob(
                id=job_id,
                stage=(
                    JobStage.TRANSCRIBING.value
                    if entrypoint == "run"
                    else JobStage.INTERRUPTED.value
                ),
            )
        )
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="f" * 64,
                duration_ms=30_000,
                speech_mapping_json=(
                    '[{"compact_start_ms":0,"compact_end_ms":30000,'
                    '"source_start_ms":0,"source_end_ms":30000}]'
                ),
                vad_speech_json='[{"start_ms":0,"end_ms":30000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        await session.commit()

    class DelayedRepeatedEngine:
        async def transcribe_file(self, file: JobFile, resume_from: int):
            for index in range(resume_from, 13):
                await asyncio.sleep(0.01)
                yield TranscriptSegment(
                    file.id,
                    index,
                    index * 2_000,
                    index * 2_000 + 1_000,
                    "重复的快速转写内容",
                    [],
                )

    class SlowRefiner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def refine(self, segment_uids: list[str]):
            self.calls.extend(segment_uids)
            await asyncio.sleep(0.05)
            uid = segment_uids[0]
            segment_index = int(uid.rsplit(":", maxsplit=1)[1])
            start_ms = segment_index * 2_000
            return [
                AlignedTranscriptSegment(
                    start_ms=start_ms,
                    end_ms=start_ms + 1_000,
                    text=f"refined {segment_index}",
                    words=(Word("refined", start_ms, start_ms + 1_000),),
                    speaker_id=None,
                )
            ]

    class RecordingRiskGate(TranscriptionRiskGateService):
        def __init__(self, database: Database) -> None:
            super().__init__(database)
            self.bulk_elapsed_seconds: float | None = None

        async def apply(
            self,
            job_id: str,
            refiner,
            *,
            bulk_elapsed_seconds: float | None = None,
        ):
            self.bulk_elapsed_seconds = bulk_elapsed_seconds
            return await super().apply(
                job_id,
                refiner,
                bulk_elapsed_seconds=bulk_elapsed_seconds,
            )

    refiner = SlowRefiner()
    risk_gate = RecordingRiskGate(database)
    service = TranscriptionService(database, risk_gate=risk_gate)
    if entrypoint == "run":
        await service.run_job(job_id, DelayedRepeatedEngine())
    else:
        await service.resume_job(job_id, DelayedRepeatedEngine())

    assert risk_gate.bulk_elapsed_seconds is not None
    assert risk_gate.bulk_elapsed_seconds > 0
    assert refiner.calls == []
    async with database.session() as session:
        transcripts = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )
    assert all(item.risk_state is None for item in transcripts)
    assert all(item.reliability_weight == 0.6 for item in transcripts[2:])
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("risk_state", ["POST_EDIT_FAILED", "HIGH_RISK_PENDING"])
async def test_unreliable_segment_keeps_timing_and_reason_without_content(
    tmp_path: Path, risk_state: str
) -> None:
    database = Database(tmp_path / "risk-state.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="test.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="b" * 64,
                duration_ms=1000,
                position=0,
                temporary_path=str(tmp_path / "test.mp3"),
            )
        )
        await session.commit()

    await TranscriptionService(database)._save_segment(
        TranscriptSegment(
            file_id=file_id,
            index=0,
            start_ms=100,
            end_ms=900,
            text="不得保留的原始转写",
            words=[{"word": "不得保留"}],
            risk_state=risk_state,
            is_reliable=False,
            reliability_weight=0.2,
            risk_reason="精转写失败",
        )
    )

    async with database.session() as session:
        stored = await session.scalar(
            select(Transcript).where(Transcript.job_file_id == file_id)
        )

    assert stored is not None
    assert stored.risk_state == risk_state
    assert stored.is_reliable is False
    assert stored.reliability_weight == 0.2
    assert stored.risk_reason == "精转写失败"
    assert (stored.start_ms, stored.end_ms) == (100, 900)
    assert stored.text == ""
    assert stored.words_json == "[]"
    await database.dispose()


@pytest.mark.asyncio
async def test_orm_rejects_content_on_an_unreliable_segment(tmp_path: Path) -> None:
    database = Database(tmp_path / "unreliable-orm.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.TRANSCRIBING.value))
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="test.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="d" * 64,
                duration_ms=1000,
                position=0,
                temporary_path=str(tmp_path / "test.mp3"),
            )
        )
        session.add(
            Transcript(
                id=str(uuid4()),
                job_file_id=file_id,
                segment_index=0,
                start_ms=0,
                end_ms=1000,
                text="不得保存",
                words_json="[]",
                is_reliable=False,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    await database.dispose()


@pytest.mark.asyncio
async def test_startup_marks_paid_work_interrupted_without_auto_resume(tmp_path: Path) -> None:
    database = Database(tmp_path / "startup.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add_all(
            [
                AnalysisJob(id=str(uuid4()), stage=JobStage.TRANSCRIBING.value),
                AnalysisJob(id=str(uuid4()), stage=JobStage.ANALYZING.value),
                AnalysisJob(id=str(uuid4()), stage=JobStage.COMPLETED.value),
            ]
        )
        await session.commit()

    changed = await TranscriptionService(database).mark_abandoned_work_interrupted()
    async with database.session() as session:
        stages = list(await session.scalars(AnalysisJob.__table__.select().with_only_columns(AnalysisJob.stage)))

    assert changed == 1
    assert stages.count(JobStage.INTERRUPTED.value) == 1
    assert stages.count(JobStage.ANALYZING.value) == 1
    assert stages.count(JobStage.COMPLETED.value) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_clears_stale_eta_samples(tmp_path: Path) -> None:
    database = Database(tmp_path / "resume-eta.sqlite3")
    await database.create_schema()
    job_id = str(uuid4())
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage=JobStage.INTERRUPTED.value))
        await session.commit()
    tracker = TranscriptionEtaTracker()
    tracker.record(job_id, 300_000, 30)
    service = TranscriptionService(
        database,
        eta_tracker=tracker,
        risk_gate=TranscriptionRiskGateService(database),
        refiner=SelectiveRefiner(database),
    )

    await service.resume_job(job_id, InterruptOnceEngine())

    assert tracker.estimate_seconds(job_id, 600_000) is None
    await database.dispose()


@pytest.mark.asyncio
async def test_ffmpeg_normalizes_audio_to_16khz_mono_pcm(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    target = tmp_path / "normalized.wav"
    subprocess_args = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.1",
        "-c:a",
        "libmp3lame",
        "-y",
        str(source),
    ]
    import subprocess

    subprocess.run(subprocess_args, check=True)

    await MLXWhisperEngine._normalize(source, target)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,codec_name",
            "-of",
            "default=noprint_wrappers=1",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "codec_name=pcm_s16le" in probe
    assert "sample_rate=16000" in probe
    assert "channels=1" in probe
