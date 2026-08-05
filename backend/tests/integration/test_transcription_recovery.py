from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, JobFile, Transcript
from audio_memory.transcription.checkpoints import TranscriptionService
from audio_memory.transcription.engine import MLXWhisperEngine
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
    service = TranscriptionService(database)
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
