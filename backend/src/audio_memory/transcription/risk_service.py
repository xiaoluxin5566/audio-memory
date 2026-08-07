from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import time
from typing import Protocol

from sqlalchemy import select

from audio_memory.db import Database
from audio_memory.diarization.alignment import AlignedTranscriptSegment
from audio_memory.models import JobFile, Transcript
from audio_memory.transcription.risk_gate import (
    EnergyInterval,
    RiskDecision,
    TimeInterval,
    classify_segments,
    normalized_similarity,
    normalize_transcript_text,
)
from audio_memory.transcription.risk_metrics import (
    RefinementWallClockBudget,
    RiskGateMetrics,
)
from audio_memory.transcription.segments import TranscriptSegment


logger = logging.getLogger(__name__)


class SegmentRefiner(Protocol):
    async def refine(
        self, segment_uids: list[str]
    ) -> list[AlignedTranscriptSegment]: ...


@dataclass(frozen=True, slots=True)
class _RiskCandidate:
    transcript_id: str
    segment_uid: str
    file_id: str
    file_position: int
    start_ms: int
    end_ms: int
    segment_index: int
    reason: str | None


class TranscriptionRiskGateService:
    """Classify persisted fast transcripts and refine only bounded high-risk work."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def apply(
        self,
        job_id: str,
        refiner: SegmentRefiner,
        *,
        bulk_elapsed_seconds: float | None = None,
    ) -> RiskGateMetrics:
        started = time.monotonic()
        async with self.database.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(Transcript, JobFile)
                        .join(JobFile, JobFile.id == Transcript.job_file_id)
                        .where(JobFile.job_id == job_id)
                        .order_by(
                            JobFile.position,
                            JobFile.id,
                            Transcript.start_ms,
                            Transcript.end_ms,
                            Transcript.segment_index,
                            Transcript.id,
                        )
                    )
                ).all()
            )

        candidates: list[_RiskCandidate] = []
        rejected = 0
        interrupted = 0
        total_segments = len(rows)
        queue_limit = max(10, math.ceil(total_segments * 0.05))
        reserved_queue_slots = sum(
            transcript.risk_state
            in {"HIGH_RISK_PENDING", "POST_EDIT_PASSED", "POST_EDIT_FAILED"}
            for transcript, _ in rows
        )
        grouped: dict[str, list[tuple[Transcript, JobFile]]] = {}
        text_contexts: dict[str, dict[str, tuple[int, int, str]]] = {}
        for transcript, file in rows:
            grouped.setdefault(file.id, []).append((transcript, file))
            if transcript.risk_state == "HIGH_RISK_PENDING":
                await self._store_refinement_failed(
                    transcript.id, "post_edit_interrupted"
                )
                interrupted += 1

        for file_rows in grouped.values():
            file = file_rows[0][1]
            eligible = [
                transcript
                for transcript, _ in file_rows
                if not transcript.risk_classified
            ]
            if not eligible:
                continue
            text_contexts[file.id] = {
                transcript.segment_uid: (
                    transcript.start_ms,
                    transcript.end_ms,
                    transcript.text,
                )
                for transcript in eligible
            }
            segments = [
                TranscriptSegment(
                    file_id=transcript.job_file_id,
                    index=transcript.segment_index,
                    start_ms=transcript.start_ms,
                    end_ms=transcript.end_ms,
                    text=transcript.text,
                    words=[],
                )
                for transcript in eligible
            ]
            decisions = classify_segments(
                segments,
                _speech_intervals(file),
                _energy_intervals(file),
            )
            for transcript, decision in zip(eligible, decisions, strict=True):
                if decision.state == "REJECTED":
                    await self._store_decision(transcript.id, decision)
                    text_contexts[file.id].pop(transcript.segment_uid, None)
                    rejected += 1
                elif decision.state == "HIGH_RISK_PENDING":
                    candidates.append(
                        _RiskCandidate(
                            transcript.id,
                            transcript.segment_uid,
                            file.id,
                            file.position,
                            transcript.start_ms,
                            transcript.end_ms,
                            transcript.segment_index,
                            decision.reason,
                        )
                    )
                else:
                    await self._store_decision(transcript.id, decision)

        available_queue_slots = max(0, queue_limit - reserved_queue_slots)
        ordered = sorted(
            candidates,
            key=lambda item: (
                item.file_position,
                item.file_id,
                item.start_ms,
                item.end_ms,
                item.segment_index,
                item.transcript_id,
            ),
        )
        queued = ordered[:available_queue_slots]
        overflowed = ordered[available_queue_slots:]
        for candidate in overflowed:
            await self._store_medium_risk(candidate.transcript_id, candidate.reason)
        passed = 0
        failed = 0
        queued_started = time.monotonic()
        admitted = 0
        budget = (
            RefinementWallClockBudget(bulk_elapsed_seconds)
            if bulk_elapsed_seconds is not None
            else None
        )
        for position, candidate in enumerate(queued):
            if budget is not None and not budget.allows_next(
                queued_elapsed_seconds=time.monotonic() - queued_started
            ):
                budget_overflow = queued[position:]
                for overflow in budget_overflow:
                    await self._store_medium_risk(overflow.transcript_id, overflow.reason)
                overflowed.extend(budget_overflow)
                break
            await self._store_pending(candidate)
            admitted += 1
            refined = await refiner.refine([candidate.segment_uid])
            result = refined[0] if len(refined) == 1 else None
            context = text_contexts[candidate.file_id]
            if (
                result is not None
                and result.words
                and result.text.strip()
                and not _text_is_repeated(result.text, candidate.segment_uid, context)
            ):
                await self._store_refinement_passed(candidate.transcript_id, result)
                context[candidate.segment_uid] = (
                    result.start_ms,
                    result.end_ms,
                    result.text,
                )
                passed += 1
            else:
                await self._store_refinement_failed(
                    candidate.transcript_id, candidate.reason
                )
                context[candidate.segment_uid] = (
                    candidate.start_ms,
                    candidate.end_ms,
                    "",
                )
                failed += 1

        metrics = RiskGateMetrics(
            total_segments=total_segments,
            rejected=rejected,
            queued=admitted,
            overflowed=len(overflowed),
            passed=passed,
            failed=failed + interrupted,
            elapsed_seconds=time.monotonic() - started,
        )
        metrics.log(logger)
        return metrics

    async def _store_decision(self, transcript_id: str, decision: RiskDecision) -> None:
        async with self.database.session() as session:
            transcript = await session.get(Transcript, transcript_id)
            if transcript is None or transcript.risk_classified:
                return
            transcript.risk_state = decision.state
            transcript.risk_classified = True
            transcript.is_reliable = decision.is_reliable
            transcript.reliability_weight = decision.reliability_weight
            transcript.risk_reason = decision.reason
            if not decision.is_reliable:
                transcript.text = ""
                transcript.words_json = "[]"
            await session.commit()

    async def _store_medium_risk(self, transcript_id: str, reason: str | None) -> None:
        async with self.database.session() as session:
            transcript = await session.get(Transcript, transcript_id)
            if transcript is None or transcript.risk_classified:
                return
            transcript.risk_state = None
            transcript.risk_classified = True
            transcript.is_reliable = True
            transcript.reliability_weight = 0.6
            transcript.risk_reason = reason
            await session.commit()

    async def _store_pending(self, candidate: _RiskCandidate) -> None:
        async with self.database.session() as session:
            transcript = await session.get(Transcript, candidate.transcript_id)
            if transcript is None or transcript.risk_classified:
                return
            transcript.risk_state = "HIGH_RISK_PENDING"
            transcript.risk_classified = True
            transcript.is_reliable = False
            transcript.reliability_weight = 0.0
            transcript.risk_reason = candidate.reason
            transcript.text = ""
            transcript.words_json = "[]"
            await session.commit()

    async def _store_refinement_passed(
        self, transcript_id: str, result: AlignedTranscriptSegment
    ) -> None:
        text = result.text.strip()
        if not text or not result.words:
            await self._store_refinement_failed(transcript_id, "post_edit_failed")
            return
        async with self.database.session() as session:
            transcript = await session.get(Transcript, transcript_id)
            if transcript is None or transcript.risk_state != "HIGH_RISK_PENDING":
                return
            transcript.risk_state = "POST_EDIT_PASSED"
            transcript.is_reliable = True
            transcript.reliability_weight = 1.0
            transcript.risk_reason = None
            transcript.text = text
            transcript.words_json = json.dumps(
                [
                    {
                        "word": word.text,
                        "start_ms": word.start_ms,
                        "end_ms": word.end_ms,
                    }
                    for word in result.words
                ],
                ensure_ascii=False,
            )
            await session.commit()

    async def _store_refinement_failed(
        self, transcript_id: str, reason: str | None
    ) -> None:
        async with self.database.session() as session:
            transcript = await session.get(Transcript, transcript_id)
            if transcript is None or transcript.risk_state != "HIGH_RISK_PENDING":
                return
            transcript.risk_state = "POST_EDIT_FAILED"
            transcript.is_reliable = False
            transcript.reliability_weight = 0.0
            transcript.risk_reason = reason or "post_edit_failed"
            transcript.text = ""
            transcript.words_json = "[]"
            await session.commit()


def _speech_intervals(file: JobFile) -> list[TimeInterval]:
    intervals = [
        TimeInterval(item["source_start_ms"], item["source_end_ms"])
        for item in _json_items(file.speech_mapping_json)
        if _valid_interval(item, "source_start_ms", "source_end_ms")
    ]
    if intervals:
        return intervals
    duration_ms = int(file.duration_ms or 0)
    return [TimeInterval(0, duration_ms)] if duration_ms > 0 else []


def _energy_intervals(file: JobFile) -> list[EnergyInterval]:
    return [
        EnergyInterval(item["start_ms"], item["end_ms"], bool(item["has_signal"]))
        for item in _json_items(file.vad_energy_json)
        if _valid_interval(item, "start_ms", "end_ms") and "has_signal" in item
    ]


def _json_items(serialized: str) -> list[dict[str, object]]:
    try:
        parsed = json.loads(serialized)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _valid_interval(item: dict[str, object], start: str, end: str) -> bool:
    return (
        isinstance(item.get(start), int)
        and isinstance(item.get(end), int)
        and int(item[start]) >= 0
        and int(item[end]) > int(item[start])
    )


def _text_is_repeated(
    text: str,
    segment_uid: str,
    context: dict[str, tuple[int, int, str]],
) -> bool:
    normalized = normalize_transcript_text(text)
    if not normalized:
        return True
    if _repeated_phrase(normalized):
        return True
    target_start_ms = context[segment_uid][0]
    nearby = 0
    for candidate_start_ms, _, candidate_text in context.values():
        if abs(target_start_ms - candidate_start_ms) <= 30_000 and (
            normalized_similarity(text, candidate_text) >= 0.90
        ):
            nearby += 1
    return nearby >= 3


def _repeated_phrase(text: str) -> bool:
    for start in range(len(text)):
        for length in range(8, (len(text) - start) // 3 + 1):
            phrase = text[start : start + length]
            if text.startswith(phrase * 3, start):
                return True
    return False
