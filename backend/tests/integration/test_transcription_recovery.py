from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from audio_memory.db import Database
from audio_memory.api.jobs import track_transcription
from audio_memory.diarization.alignment import AlignedTranscriptSegment, Word
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, JobFile, Transcript
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
    await service.run_job(job_id, engine)

    async with database.session() as session:
        rows = list(await session.scalars(Transcript.__table__.select()))
        job = await session.get(AnalysisJob, job_id)
    assert engine.calls == [(file_id, 0), (file_id, 1)]
    assert len(rows) == 3
    assert job.stage == JobStage.ANALYZING.value
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

    assert changed == 2
    assert stages.count(JobStage.INTERRUPTED.value) == 2
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
