from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata

from audio_memory.transcription.compact import CompactBatch, CompactEntry


@dataclass(frozen=True, slots=True)
class MappingRejection:
    reason: str


@dataclass(frozen=True, slots=True)
class MappedSegment:
    batch_index: int
    start_ms: int
    end_ms: int
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    wholly_mapped: bool = True


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    kept: tuple[MappedSegment, ...]
    rejected: tuple[MappedSegment, ...]
    conflict_count: int


def _number(raw: dict[str, object], key: str) -> float | None:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def map_segment(
    batch: CompactBatch,
    raw_segment: dict[str, object],
    *,
    tolerance_ms: int = 300,
) -> MappedSegment | MappingRejection:
    text = str(raw_segment.get("text", "")).strip()
    if not text:
        return MappingRejection("empty_text")
    start_value = _number(raw_segment, "start_ms")
    end_value = _number(raw_segment, "end_ms")
    if (
        start_value is None
        or end_value is None
        or start_value < 0
        or end_value <= start_value
    ):
        return MappingRejection("invalid_time")
    start_ms = round(start_value)
    end_ms = round(end_value)
    if start_ms >= batch.compact_ms or end_ms <= 0:
        return MappingRejection("outside_batch")

    source_entries = [item for item in batch.entries if item.kind == "source"]
    overlapping = [
        item
        for item in source_entries
        if min(end_ms, item.compact_end_ms) > max(start_ms, item.compact_start_ms)
    ]
    if not overlapping:
        return MappingRejection("separator_only")
    if len(overlapping) > 1:
        return MappingRejection("cross_source_entry")

    entry = overlapping[0]
    later_sources = [
        item for item in source_entries if item.compact_start_ms > entry.compact_start_ms
    ]
    if later_sources and end_ms >= later_sources[0].compact_start_ms:
        return MappingRejection("cross_source_entry")
    before_ms = max(0, entry.compact_start_ms - start_ms)
    after_ms = max(0, end_ms - entry.compact_end_ms)
    if before_ms > tolerance_ms or after_ms > tolerance_ms:
        return MappingRejection("severe_boundary_overrun")

    clipped_start = max(start_ms, entry.compact_start_ms)
    clipped_end = min(end_ms, entry.compact_end_ms)
    assert entry.source_start_ms is not None
    source_start_ms = entry.source_start_ms + clipped_start - entry.compact_start_ms
    source_end_ms = entry.source_start_ms + clipped_end - entry.compact_start_ms
    avg_logprob = _number(raw_segment, "avg_logprob")
    no_speech_prob = _number(raw_segment, "no_speech_prob")
    return MappedSegment(
        batch_index=batch.index,
        start_ms=source_start_ms,
        end_ms=source_end_ms,
        text=text,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
        wholly_mapped=before_ms == 0 and after_ms == 0,
    )


def _normalized_text(text: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", text)
        if character.isalnum()
    )


def _protected_tokens(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    numbers = tuple(re.findall(r"\d+(?:[.:/-]\d+)*", normalized))
    negations = tuple(re.findall(r"不|没|未|无|非|否|not|never|no", normalized))
    return numbers, negations


def _overlap_ratio(left: MappedSegment, right: MappedSegment) -> float:
    overlap_ms = max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))
    shorter_ms = min(left.end_ms - left.start_ms, right.end_ms - right.start_ms)
    return overlap_ms / shorter_ms if shorter_ms > 0 else 0.0


def _rank(segment: MappedSegment, stable_order: int) -> tuple[object, ...]:
    return (
        segment.wholly_mapped,
        segment.avg_logprob if segment.avg_logprob is not None else float("-inf"),
        -(segment.no_speech_prob if segment.no_speech_prob is not None else float("inf")),
        len(segment.text.strip()),
        -stable_order,
    )


def reconcile_mapped_segments(
    segments: list[MappedSegment] | tuple[MappedSegment, ...],
    *,
    duplicate_overlap_ratio: float = 0.3,
) -> ReconciliationResult:
    kept: list[tuple[int, MappedSegment]] = []
    rejected: list[MappedSegment] = []
    conflict_count = 0

    for stable_order, segment in enumerate(segments):
        if (
            segment.start_ms < 0
            or segment.end_ms <= segment.start_ms
            or not segment.text.strip()
        ):
            rejected.append(segment)
            continue
        match_index = next(
            (
                index
                for index, (_, candidate) in enumerate(kept)
                if _overlap_ratio(candidate, segment) >= duplicate_overlap_ratio
            ),
            None,
        )
        if match_index is None:
            kept.append((stable_order, segment))
            continue

        candidate_order, candidate = kept[match_index]
        is_duplicate = (
            _normalized_text(candidate.text) == _normalized_text(segment.text)
            and _protected_tokens(candidate.text) == _protected_tokens(segment.text)
        )
        if not is_duplicate:
            conflict_count += 1
        if _rank(segment, stable_order) > _rank(candidate, candidate_order):
            rejected.append(candidate)
            kept[match_index] = (stable_order, segment)
        else:
            rejected.append(segment)

    ordered_kept = tuple(
        segment for _, segment in sorted(kept, key=lambda item: (item[1].start_ms, item[0]))
    )
    return ReconciliationResult(ordered_kept, tuple(rejected), conflict_count)
