from __future__ import annotations

import json
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select, update

from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, JobFile, Transcript
from audio_memory.transcription.segments import TranscriptSegment


class TranscriptionEngine(Protocol):
    def transcribe_file(self, file: JobFile, resume_from: int): ...


class TranscriptionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def run_job(self, job_id: str, engine: TranscriptionEngine) -> None:
        files = await self._files(job_id)
        try:
            for file in files:
                resume_from = await self._resume_index(file.id)
                async for segment in engine.transcribe_file(file, resume_from):
                    await self._save_segment(segment)
        except BaseException:
            await self._set_stage(job_id, JobStage.INTERRUPTED)
            raise
        await self._set_stage(job_id, JobStage.ANALYZING)

    async def resume_job(self, job_id: str, engine: TranscriptionEngine) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            if job.stage != JobStage.INTERRUPTED.value:
                raise ValueError("Only an interrupted transcription can resume")
            job.stage = JobStage.TRANSCRIBING.value
            await session.commit()
        await self.run_job(job_id, engine)

    async def mark_abandoned_work_interrupted(self) -> int:
        async with self.database.session() as session:
            result = await session.execute(
                update(AnalysisJob)
                .where(
                    AnalysisJob.stage.in_(
                        [JobStage.TRANSCRIBING.value, JobStage.ANALYZING.value]
                    )
                )
                .values(stage=JobStage.INTERRUPTED.value)
            )
            await session.commit()
            return int(result.rowcount)

    async def _files(self, job_id: str) -> list[JobFile]:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            rows = await session.scalars(
                select(JobFile)
                .where(JobFile.job_id == job_id)
                .order_by(JobFile.position)
            )
            return list(rows)

    async def _resume_index(self, file_id: str) -> int:
        async with self.database.session() as session:
            maximum = await session.scalar(
                select(func.max(Transcript.segment_index)).where(
                    Transcript.job_file_id == file_id
                )
            )
            return int(maximum if maximum is not None else -1) + 1

    async def _save_segment(self, segment: TranscriptSegment) -> None:
        async with self.database.session() as session:
            session.add(
                Transcript(
                    id=str(uuid4()),
                    job_file_id=segment.file_id,
                    segment_index=segment.index,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text.strip(),
                    words_json=json.dumps(segment.words, ensure_ascii=False),
                )
            )
            await session.commit()

    async def _set_stage(self, job_id: str, stage: JobStage) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            job.stage = stage.value
            await session.commit()
