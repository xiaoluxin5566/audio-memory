from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
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
    MAX_COMPARISON_TEXT_CHARS,
    MAX_NEARBY_COMPARISONS,
    REPEAT_EVIDENCE_REJECTION_REASONS,
    RiskDecision,
    TimeInterval,
    adjacent_phrase_repetitions,
    classify_segments,
    normalized_lengths_can_be_similar,
    normalized_texts_are_similar,
    normalize_transcript_text,
)
from audio_memory.transcription.risk_metrics import (
    RefinementWallClockBudget,
    RiskGateMetrics,
)
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
    text: str
    words_json: str


@dataclass(frozen=True, slots=True)
class _SnapshotSegment:
    """Immutable copy of one persisted fast transcript used for classification."""

    transcript_id: str
    segment_uid: str
    file_id: str
    index: int
    start_ms: int
    end_ms: int
    text: str
    words_json: str


@dataclass(frozen=True, slots=True)
class _FileRiskSnapshot:
    file_id: str
    file_position: int
    owning_window: TimeInterval | None
    speech_intervals: tuple[TimeInterval, ...]
    energy_intervals: tuple[EnergyInterval, ...]
    vad_available: bool
    segments: tuple[_SnapshotSegment, ...]
    context_complete: bool


@dataclass(frozen=True, slots=True)
class _PlannedDecision:
    transcript_id: str
    state: str | None
    reason: str | None
    is_reliable: bool
    reliability_weight: float
    text: str
    words_json: str


@dataclass(slots=True)
class _TextContext:
    entries: dict[str, tuple[int, int, str]]
    ordered: tuple[tuple[int, str], ...]
    starts: tuple[int, ...]
    normalized_by_uid: dict[str, str]
    exact: dict[str, list[tuple[int, str]]]

    @classmethod
    def from_entries(
        cls, entries: dict[str, tuple[int, int, str]]
    ) -> _TextContext:
        ordered = tuple(
            sorted((start_ms, uid) for uid, (start_ms, _, _) in entries.items())
        )
        normalized_by_uid = {
            uid: normalize_transcript_text(text)
            for uid, (_, _, text) in entries.items()
        }
        exact: dict[str, list[tuple[int, str]]] = {}
        for start_ms, uid in ordered:
            exact.setdefault(normalized_by_uid[uid], []).append((start_ms, uid))
        return cls(
            entries,
            ordered,
            tuple(item[0] for item in ordered),
            normalized_by_uid,
            exact,
        )

    def replace(self, uid: str, text: str) -> None:
        start_ms, end_ms, _ = self.entries[uid]
        old_normalized = self.normalized_by_uid[uid]
        old_item = (start_ms, uid)
        old_exact = self.exact[old_normalized]
        old_exact.pop(bisect_left(old_exact, old_item))
        if not old_exact:
            self.exact.pop(old_normalized)
        new_normalized = normalize_transcript_text(text)
        self.entries[uid] = (start_ms, end_ms, text)
        self.normalized_by_uid[uid] = new_normalized
        insort(self.exact.setdefault(new_normalized, []), old_item)

    def exact_nearby_count(
        self,
        normalized: str,
        target_start_ms: int,
        excluded_uid: str,
    ) -> int:
        matches = self.exact.get(normalized, [])
        lower = bisect_left(matches, (target_start_ms - 30_000, ""))
        upper = bisect_right(matches, (target_start_ms + 30_000, "\U0010ffff"))
        count = 0
        for index in range(lower, upper):
            if matches[index][1] == excluded_uid:
                continue
            count += 1
            if count >= 2:
                break
        return count

    def nearby_normalized_texts(
        self,
        target_start_ms: int,
        excluded_uid: str,
    ) -> list[str]:
        lower = bisect_left(self.starts, target_start_ms - 30_000)
        upper = bisect_right(self.starts, target_start_ms + 30_000)
        return [
            self.normalized_by_uid[uid]
            for _, uid in self.ordered[lower:upper]
            if uid != excluded_uid
        ]


class TranscriptionRiskGateService:
    """Classify persisted fast transcripts and refine only bounded high-risk work."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def apply(
        self,
        job_id: str,
        refiner: SegmentRefiner | None = None,
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

        rejected = 0
        total_segments = len(rows)
        queue_limit = max(10, math.ceil(total_segments * 0.05))
        reserved_queue_slots = sum(
            transcript.risk_state
            in {"HIGH_RISK_PENDING", "POST_EDIT_PASSED", "POST_EDIT_FAILED"}
            for transcript, _ in rows
        )
        interrupted_ids = [
            transcript.id
            for transcript, _ in rows
            if transcript.risk_state == "HIGH_RISK_PENDING"
        ]
        if interrupted_ids:
            await self._store_interrupted_failures(interrupted_ids)
        interrupted = len(interrupted_ids)

        grouped: dict[str, list[tuple[Transcript, JobFile]]] = {}
        for transcript, file in rows:
            grouped.setdefault(file.id, []).append((transcript, file))

        snapshots = [
            snapshot
            for file_rows in grouped.values()
            if (snapshot := _file_snapshot(file_rows)) is not None
        ]
        classified: dict[
            str, tuple[_FileRiskSnapshot, tuple[RiskDecision, ...]]
        ] = {}
        candidates: list[_RiskCandidate] = []
        text_contexts: dict[str, _TextContext] = {}
        for snapshot in snapshots:
            if snapshot.context_complete:
                decisions = tuple(
                    classify_segments(
                        snapshot.segments,
                        snapshot.speech_intervals,
                        snapshot.energy_intervals,
                        owning_window=snapshot.owning_window,
                        vad_available=snapshot.vad_available,
                    )
                )
            else:
                decisions = tuple(
                    RiskDecision(
                        segment.index,
                        "REJECTED",
                        "classification_context_incomplete",
                        False,
                        0.0,
                    )
                    for segment in snapshot.segments
                )
            classified[snapshot.file_id] = (snapshot, decisions)
            text_contexts[snapshot.file_id] = _TextContext.from_entries(
                {
                    segment.segment_uid: (
                        segment.start_ms,
                        segment.end_ms,
                        segment.text,
                    )
                    for segment, decision in zip(
                        snapshot.segments, decisions, strict=True
                    )
                    if decision.state != "REJECTED"
                    or decision.reason in REPEAT_EVIDENCE_REJECTION_REASONS
                }
            )
            for segment, decision in zip(snapshot.segments, decisions, strict=True):
                if decision.state == "REJECTED":
                    rejected += 1
                elif decision.state == "HIGH_RISK_PENDING":
                    candidates.append(
                        _RiskCandidate(
                            segment.transcript_id,
                            segment.segment_uid,
                            snapshot.file_id,
                            snapshot.file_position,
                            segment.start_ms,
                            segment.end_ms,
                            segment.index,
                            decision.reason,
                            segment.text,
                            segment.words_json,
                        )
                    )

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
        budget = (
            RefinementWallClockBudget(bulk_elapsed_seconds)
            if bulk_elapsed_seconds is not None
            else None
        )
        if budget is not None and not budget.allows_next(
            queued_elapsed_seconds=time.monotonic() - started
        ):
            overflowed.extend(queued)
            queued = []

        queued_ids = {candidate.transcript_id for candidate in queued}
        for snapshot, decisions in classified.values():
            plans = tuple(
                _planned_decision(
                    segment,
                    decision,
                    admitted=segment.transcript_id in queued_ids,
                )
                for segment, decision in zip(
                    snapshot.segments, decisions, strict=True
                )
            )
            await self._store_file_classification(snapshot.file_id, plans)

        passed = 0
        failed = 0
        admitted = 0
        for position, candidate in enumerate(queued):
            if refiner is None:
                await self._downgrade_pending(queued[position:])
                overflowed.extend(queued[position:])
                break
            if budget is not None and not budget.allows_next(
                queued_elapsed_seconds=time.monotonic() - started
            ):
                budget_overflow = queued[position:]
                await self._downgrade_pending(budget_overflow)
                overflowed.extend(budget_overflow)
                break
            admitted += 1
            refined = await refiner.refine([candidate.segment_uid])
            result = refined[0] if len(refined) == 1 else None
            context = text_contexts[candidate.file_id]
            if (
                result is not None
                and result.words
                and result.text.strip()
                and not _text_is_repeated(
                    result.text,
                    candidate.segment_uid,
                    candidate.start_ms,
                    context,
                )
            ):
                await self._store_refinement_passed(candidate.transcript_id, result)
                context.replace(candidate.segment_uid, result.text)
                passed += 1
            else:
                await self._store_refinement_failed(
                    candidate.transcript_id, candidate.reason
                )
                context.replace(candidate.segment_uid, "")
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

    async def _store_interrupted_failures(
        self, transcript_ids: list[str]
    ) -> None:
        async with self.database.session() as session:
            transcripts = list(
                await session.scalars(
                    select(Transcript).where(Transcript.id.in_(transcript_ids))
                )
            )
            for transcript in transcripts:
                if transcript.risk_state != "HIGH_RISK_PENDING":
                    continue
                transcript.risk_state = "POST_EDIT_FAILED"
                transcript.is_reliable = False
                transcript.reliability_weight = 0.0
                transcript.risk_reason = "post_edit_interrupted"
                transcript.text = ""
                transcript.words_json = "[]"
            await session.commit()

    async def _store_file_classification(
        self,
        file_id: str,
        plans: tuple[_PlannedDecision, ...],
    ) -> None:
        """Persist one immutable file snapshot in a single transaction."""
        if not plans:
            return
        async with self.database.session() as session:
            transcripts = list(
                await session.scalars(
                    select(Transcript)
                    .where(
                        Transcript.job_file_id == file_id,
                        Transcript.id.in_([plan.transcript_id for plan in plans]),
                    )
                    .order_by(Transcript.id)
                )
            )
            by_id = {transcript.id: transcript for transcript in transcripts}
            if len(by_id) != len(plans) or any(
                by_id[plan.transcript_id].risk_classified for plan in plans
            ):
                raise RuntimeError("Risk classification snapshot changed")
            for plan in plans:
                transcript = by_id[plan.transcript_id]
                transcript.risk_state = plan.state
                transcript.risk_classified = True
                transcript.is_reliable = plan.is_reliable
                transcript.reliability_weight = plan.reliability_weight
                transcript.risk_reason = plan.reason
                transcript.text = plan.text
                transcript.words_json = plan.words_json
            await session.commit()

    async def _downgrade_pending(
        self, candidates: list[_RiskCandidate]
    ) -> None:
        if not candidates:
            return
        async with self.database.session() as session:
            transcripts = list(
                await session.scalars(
                    select(Transcript).where(
                        Transcript.id.in_(
                            [candidate.transcript_id for candidate in candidates]
                        )
                    )
                )
            )
            by_id = {transcript.id: transcript for transcript in transcripts}
            for candidate in candidates:
                transcript = by_id.get(candidate.transcript_id)
                if transcript is None or transcript.risk_state != "HIGH_RISK_PENDING":
                    continue
                transcript.risk_state = None
                transcript.is_reliable = True
                transcript.reliability_weight = 0.6
                transcript.risk_reason = candidate.reason
                transcript.text = candidate.text
                transcript.words_json = candidate.words_json
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


def _file_snapshot(
    file_rows: list[tuple[Transcript, JobFile]],
) -> _FileRiskSnapshot | None:
    eligible = [transcript for transcript, _ in file_rows if not transcript.risk_classified]
    if not eligible:
        return None
    file = file_rows[0][1]
    duration_ms = int(file.duration_ms or 0)
    speech_intervals = tuple(_speech_intervals(file))
    return _FileRiskSnapshot(
        file_id=file.id,
        file_position=file.position,
        owning_window=(
            TimeInterval(0, duration_ms) if duration_ms > 0 else None
        ),
        speech_intervals=speech_intervals,
        energy_intervals=tuple(_energy_intervals(file)),
        vad_available=bool(file.vad_available or speech_intervals),
        segments=tuple(
            _SnapshotSegment(
                transcript_id=transcript.id,
                segment_uid=transcript.segment_uid,
                file_id=transcript.job_file_id,
                index=transcript.segment_index,
                start_ms=transcript.start_ms,
                end_ms=transcript.end_ms,
                text=transcript.text,
                words_json=transcript.words_json,
            )
            for transcript in eligible
        ),
        context_complete=len(eligible) == len(file_rows),
    )


def _planned_decision(
    segment: _SnapshotSegment,
    decision: RiskDecision,
    *,
    admitted: bool,
) -> _PlannedDecision:
    if decision.state == "HIGH_RISK_PENDING" and not admitted:
        return _PlannedDecision(
            segment.transcript_id,
            None,
            decision.reason,
            True,
            0.6,
            segment.text,
            segment.words_json,
        )
    discard_content = decision.state in {"REJECTED", "HIGH_RISK_PENDING"}
    return _PlannedDecision(
        segment.transcript_id,
        decision.state,
        decision.reason,
        decision.is_reliable,
        decision.reliability_weight,
        "" if discard_content else segment.text,
        "[]" if discard_content else segment.words_json,
    )


def _speech_intervals(file: JobFile) -> list[TimeInterval]:
    return [
        TimeInterval(item["start_ms"], item["end_ms"])
        for item in _json_items(file.vad_speech_json)
        if _valid_interval(item, "start_ms", "end_ms")
    ]


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
    target_start_ms: int,
    context: _TextContext,
) -> bool:
    normalized = normalize_transcript_text(text)
    if not normalized:
        return True
    if len(normalized) > MAX_COMPARISON_TEXT_CHARS:
        return True
    if adjacent_phrase_repetitions(normalized) >= 3:
        return True
    nearby = 1 + context.exact_nearby_count(
        normalized, target_start_ms, segment_uid
    )
    if nearby >= 3:
        return True
    approximate_candidates = [
        candidate_normalized
        for candidate_normalized in context.nearby_normalized_texts(
            target_start_ms, segment_uid
        )
        if candidate_normalized != normalized
        and normalized_lengths_can_be_similar(normalized, candidate_normalized)
    ]
    if len(approximate_candidates) > MAX_NEARBY_COMPARISONS:
        return True
    for candidate_normalized in approximate_candidates:
        if normalized_texts_are_similar(normalized, candidate_normalized):
            nearby += 1
            if nearby >= 3:
                return True
    return nearby >= 3
