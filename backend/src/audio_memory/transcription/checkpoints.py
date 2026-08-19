from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select, update

from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, JobFile, Transcript
from audio_memory.transcription.segments import TranscriptSegment
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.transcription.risk_service import (
    SegmentRefiner,
    TranscriptionRiskGateService,
)


logger = logging.getLogger(__name__)


class TranscriptionEngine(Protocol):
    def transcribe_file(self, file: JobFile, resume_from: int): ...


class TranscriptionService:
    def __init__(
        self,
        database: Database,
        *,
        eta_tracker: TranscriptionEtaTracker | None = None,
        risk_gate: TranscriptionRiskGateService | None = None,
        refiner: SegmentRefiner | None = None,
    ) -> None:
        self.database = database
        self.eta_tracker = eta_tracker or TranscriptionEtaTracker()
        self.risk_gate = risk_gate
        self.refiner = refiner

    async def run_job(self, job_id: str, engine: TranscriptionEngine) -> None:
        files = await self._files(job_id)
        try:
            bulk_started = time.monotonic()
            for file in files:
                resume_from = await self._resume_index(file.id)
                async for segment in engine.transcribe_file(file, resume_from):
                    await self._save_segment(segment)
            bulk_elapsed_seconds = time.monotonic() - bulk_started
            if self.risk_gate is None:
                raise RuntimeError("Transcription risk gate is required")
            await self.risk_gate.apply(
                job_id,
                self.refiner,
                bulk_elapsed_seconds=bulk_elapsed_seconds,
            )
        except asyncio.CancelledError:
            self.eta_tracker.clear(job_id)
            await self._set_stage(job_id, JobStage.INTERRUPTED)
            raise
        except Exception as error:
            self.eta_tracker.clear(job_id)
            logger.error(
                "Local transcription failed job_id=%s "
                "diagnostic=transcription_failed error_type=%s",
                job_id,
                type(error).__name__,
            )
            await self._set_stage(
                job_id, JobStage.INTERRUPTED, error_code="transcription_failed"
            )
            raise
        self.eta_tracker.clear(job_id)

    async def resume_job(self, job_id: str, engine: TranscriptionEngine) -> None:
        self.eta_tracker.clear(job_id)
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            if job.stage != JobStage.INTERRUPTED.value:
                raise ValueError("Only an interrupted transcription can resume")
            job.stage = JobStage.TRANSCRIBING.value
            job.error_code = None
            await session.commit()
        await self.run_job(job_id, engine)

    async def mark_abandoned_work_interrupted(self) -> int:
        async with self.database.session() as session:
            result = await session.execute(
                update(AnalysisJob)
                .where(AnalysisJob.stage == JobStage.TRANSCRIBING.value)
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
        discard_content = (
            not segment.is_reliable
            or segment.risk_state
            in {"REJECTED", "HIGH_RISK_PENDING", "POST_EDIT_FAILED"}
        )
        async with self.database.session() as session:
            session.add(
                Transcript(
                    id=str(uuid4()),
                    job_file_id=segment.file_id,
                    segment_index=segment.index,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text="" if discard_content else segment.text.strip(),
                    words_json=(
                        "[]"
                        if discard_content
                        else json.dumps(segment.words, ensure_ascii=False)
                    ),
                    no_speech_prob=segment.no_speech_prob,
                    avg_logprob=segment.avg_logprob,
                    speaker_id=getattr(segment, "speaker_id", "unknown") or "unknown",
                    risk_state=segment.risk_state,
                    risk_classified=False,
                    is_reliable=segment.is_reliable,
                    reliability_weight=segment.reliability_weight,
                    risk_reason=segment.risk_reason,
                )
            )
            await session.commit()

    async def _set_stage(
        self, job_id: str, stage: JobStage, *, error_code: str | None = None
    ) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            job.stage = stage.value
            job.error_code = error_code
            await session.commit()
