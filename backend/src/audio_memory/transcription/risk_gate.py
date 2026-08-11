from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Sequence
import re
import unicodedata

from audio_memory.transcription.segments import TranscriptSegment


SIMILARITY_THRESHOLD = 0.90
VAD_GRACE_MS = 300
MIN_VAD_OVERLAP_MS = 500
MIN_VAD_COVERAGE = 0.30
NEARBY_REPEAT_WINDOW_MS = 30_000
SILENCE_GAP_MS = 10_000
SILENCE_ENERGY_COVERAGE = 0.80
MIN_SPEECH_DURATION_MS = 1_500
MAX_CHARS_PER_SECOND = 14
MAX_WORDS_PER_SECOND = 7
MEDIUM_RISK_WEIGHT = 0.6
MAX_COMPARISON_TEXT_CHARS = 1_024
MAX_NEARBY_COMPARISONS = 256
REPEAT_EVIDENCE_REJECTION_REASONS = frozenset(
    {"comparison_text_too_long", "similarity_comparison_budget_exhausted"}
)

_CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CHINESE_SMALL_UNITS = {"十": 10, "百": 100, "千": 1_000}
_CHINESE_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}
_CHINESE_NUMBER_RE = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+")
_ARABIC_NUMBER_RE = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class TimeInterval:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Interval timestamps must be increasing")


@dataclass(frozen=True, slots=True)
class EnergyInterval(TimeInterval):
    has_signal: bool


@dataclass(frozen=True, slots=True)
class RiskDecision:
    segment_index: int
    state: str | None
    reason: str | None
    is_reliable: bool
    reliability_weight: float


def normalize_transcript_text(text: str) -> str:
    """Return the deterministic form used by every repetition comparison."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _CHINESE_NUMBER_RE.sub(_parse_chinese_number, normalized)
    normalized = _ARABIC_NUMBER_RE.sub(_normalize_arabic_number, normalized)
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def normalized_similarity(first: str, second: str) -> float:
    first_normalized = normalize_transcript_text(first)
    second_normalized = normalize_transcript_text(second)
    if not first_normalized and not second_normalized:
        return 0.0
    return 1 - _levenshtein_distance(first_normalized, second_normalized) / max(
        len(first_normalized), len(second_normalized)
    )


def classify_segments(
    segments: Sequence[TranscriptSegment],
    speech_intervals: Sequence[TimeInterval],
    energy_intervals: Sequence[EnergyInterval],
    *,
    owning_window: TimeInterval | None = None,
    vad_available: bool = True,
) -> list[RiskDecision]:
    """Classify transcript candidates without retaining their text in decisions."""
    decisions: list[RiskDecision | None] = [None] * len(segments)
    ordered = sorted(enumerate(segments), key=lambda item: (item[1].start_ms, item[1].end_ms, item[0]))
    conflicting_positions = _conflicting_segment_positions(ordered, owning_window)
    prior_window: list[tuple[TranscriptSegment, str]] = []
    exact_history: dict[str, deque[int]] = {}
    previous: TranscriptSegment | None = None

    for original_position, segment in ordered:
        invalid_reason = _hard_rejection_reason(
            segment,
            speech_intervals,
            owning_window=owning_window,
            has_time_conflict=original_position in conflicting_positions,
            vad_available=vad_available,
        )
        if invalid_reason is not None:
            decisions[original_position] = _rejected(segment.index, invalid_reason)
            continue
        if original_position in conflicting_positions:
            decisions[original_position] = _medium_risk(segment.index, "timestamp_conflict")
            continue

        normalized_text = normalize_transcript_text(segment.text)
        prior_window = [
            item
            for item in prior_window
            if segment.start_ms - item[0].start_ms <= NEARBY_REPEAT_WINDOW_MS
        ]
        if len(normalized_text) > MAX_COMPARISON_TEXT_CHARS:
            decisions[original_position] = _medium_risk(
                segment.index, "comparison_text_too_long"
            )
            prior_window.append((segment, normalized_text))
            continue
        exact_starts = exact_history.setdefault(normalized_text, deque())
        while (
            exact_starts
            and segment.start_ms - exact_starts[0] > NEARBY_REPEAT_WINDOW_MS
        ):
            exact_starts.popleft()
        exact_match_count = len(exact_starts)
        approximate_match_count = 0
        if exact_match_count < 2:
            approximate_candidates = [
                candidate_normalized
                for _candidate, candidate_normalized in prior_window
                if candidate_normalized != normalized_text
                and normalized_lengths_can_be_similar(
                    normalized_text, candidate_normalized
                )
            ]
            if len(approximate_candidates) > MAX_NEARBY_COMPARISONS:
                decisions[original_position] = _medium_risk(
                    segment.index, "similarity_comparison_budget_exhausted"
                )
                prior_window.append((segment, normalized_text))
                exact_starts.append(segment.start_ms)
                continue
            approximate_match_count = sum(
                normalized_texts_are_similar(
                    normalized_text, candidate_normalized
                )
                for candidate_normalized in approximate_candidates
            )
        nearby_match_count = exact_match_count + approximate_match_count
        phrase_repetitions = adjacent_phrase_repetitions(normalized_text)
        light_repetition = nearby_match_count > 0 or phrase_repetitions >= 2

        if nearby_match_count >= 2:
            decisions[original_position] = _medium_risk(segment.index, "repeated_nearby")
        elif phrase_repetitions >= 3:
            decisions[original_position] = _medium_risk(segment.index, "repeated_phrase")
        elif _is_post_silence_repeat(segment, previous, energy_intervals):
            decisions[original_position] = _medium_risk(segment.index, "post_silence_repeat")
        elif light_repetition and _has_implausible_speech_rate(segment, speech_intervals):
            decisions[original_position] = _medium_risk(segment.index, "implausible_speech_rate")
        elif light_repetition:
            decisions[original_position] = _medium_risk(segment.index, "light_repetition")
        elif not vad_available:
            decisions[original_position] = _medium_risk(segment.index, "vad_unavailable")
        elif _has_low_vad_support(segment, speech_intervals):
            decisions[original_position] = _medium_risk(segment.index, "no_vad_support")
        else:
            decisions[original_position] = _normal(segment.index)

        prior_window.append((segment, normalized_text))
        exact_starts.append(segment.start_ms)
        previous = segment

    return [decision for decision in decisions if decision is not None]


def _parse_chinese_number(match: re.Match[str]) -> str:
    token = match.group(0)
    value = _chinese_integer(token)
    return token if value is None else str(value)


def _normalize_arabic_number(match: re.Match[str]) -> str:
    return str(int(match.group(0)))


def _chinese_integer(token: str) -> int | None:
    if all(character in _CHINESE_DIGITS for character in token):
        return int("".join(str(_CHINESE_DIGITS[character]) for character in token))

    total = 0
    section = 0
    number: int | None = None
    last_small_unit = 10_000
    for character in token:
        if character in _CHINESE_DIGITS:
            if number not in (None, 0):
                return None
            number = _CHINESE_DIGITS[character]
        elif character in _CHINESE_SMALL_UNITS:
            unit = _CHINESE_SMALL_UNITS[character]
            if unit >= last_small_unit:
                return None
            section += (1 if number is None else number) * unit
            number = None
            last_small_unit = unit
        elif character in _CHINESE_LARGE_UNITS:
            if section == 0 and number is None:
                return None
            section += 0 if number is None else number
            total += section * _CHINESE_LARGE_UNITS[character]
            section = 0
            number = None
            last_small_unit = 10_000
        else:
            return None

    value = total + section + (0 if number is None else number)
    return value if 0 <= value <= 999_999_999 else None


def _levenshtein_distance(first: str, second: str) -> int:
    if len(first) < len(second):
        first, second = second, first
    previous = list(range(len(second) + 1))
    for first_index, first_character in enumerate(first, start=1):
        current = [first_index]
        for second_index, second_character in enumerate(second, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[second_index] + 1,
                    previous[second_index - 1]
                    + (first_character != second_character),
                )
            )
        previous = current
    return previous[-1]


def _normalized_similarity_values(first: str, second: str) -> float:
    if not first and not second:
        return 0.0
    return 1 - _levenshtein_distance(first, second) / max(len(first), len(second))


def normalized_lengths_can_be_similar(first: str, second: str) -> bool:
    if not first or not second:
        return False
    maximum_length = max(len(first), len(second))
    maximum_distance = int(
        (1.0 - SIMILARITY_THRESHOLD) * maximum_length + 1e-9
    )
    return abs(len(first) - len(second)) <= maximum_distance


def normalized_texts_are_similar(first: str, second: str) -> bool:
    """Apply the 0.90 threshold with a length band and early exit."""
    if not normalized_lengths_can_be_similar(first, second):
        return False
    maximum_length = max(len(first), len(second))
    maximum_distance = int(
        (1.0 - SIMILARITY_THRESHOLD) * maximum_length + 1e-9
    )
    return _levenshtein_within_distance(first, second, maximum_distance)


def _levenshtein_within_distance(
    first: str, second: str, maximum_distance: int
) -> bool:
    if first == second:
        return True
    if abs(len(first) - len(second)) > maximum_distance:
        return False
    if len(first) < len(second):
        first, second = second, first

    unreachable = maximum_distance + 1
    previous = [unreachable] * (len(second) + 1)
    for index in range(min(len(second), maximum_distance) + 1):
        previous[index] = index

    for first_index, first_character in enumerate(first, start=1):
        lower = max(1, first_index - maximum_distance)
        upper = min(len(second), first_index + maximum_distance)
        if lower > upper:
            return False
        current = [unreachable] * (len(second) + 1)
        if first_index <= maximum_distance:
            current[0] = first_index
        row_minimum = unreachable
        for second_index in range(lower, upper + 1):
            current[second_index] = min(
                previous[second_index] + 1,
                current[second_index - 1] + 1,
                previous[second_index - 1]
                + (first_character != second[second_index - 1]),
            )
            row_minimum = min(row_minimum, current[second_index])
        if row_minimum > maximum_distance:
            return False
        previous = current
    return previous[len(second)] <= maximum_distance


def _conflicting_segment_positions(
    ordered: Sequence[tuple[int, TranscriptSegment]],
    owning_window: TimeInterval | None,
) -> set[int]:
    """Identify every well-formed segment participating in a remaining overlap."""
    conflicts: set[int] = set()
    furthest_end = -1
    furthest_position: int | None = None
    for original_position, segment in ordered:
        if (
            segment.start_ms < 0
            or segment.end_ms <= segment.start_ms
            or not segment.text.strip()
            or (
                owning_window is not None
                and (
                    segment.start_ms < owning_window.start_ms
                    or segment.end_ms > owning_window.end_ms
                )
            )
        ):
            continue
        if furthest_position is not None and segment.start_ms < furthest_end:
            conflicts.add(furthest_position)
            conflicts.add(original_position)
        if segment.end_ms > furthest_end:
            furthest_end = segment.end_ms
            furthest_position = original_position
    return conflicts


def _hard_rejection_reason(
    segment: TranscriptSegment,
    speech_intervals: Sequence[TimeInterval],
    *,
    owning_window: TimeInterval | None,
    has_time_conflict: bool,
    vad_available: bool,
) -> str | None:
    if owning_window is not None and (
        segment.start_ms < owning_window.start_ms
        or segment.end_ms > owning_window.end_ms
    ):
        return "outside_file_window"
    if segment.start_ms < 0 or segment.end_ms <= segment.start_ms:
        return "invalid_timing"
    if not segment.text.strip():
        return "blank_text"
    return None


def _has_low_vad_support(
    segment: TranscriptSegment, speech_intervals: Sequence[TimeInterval]
) -> bool:
    duration = segment.end_ms - segment.start_ms
    expanded = [
        TimeInterval(max(0, interval.start_ms - VAD_GRACE_MS), interval.end_ms + VAD_GRACE_MS)
        for interval in speech_intervals
    ]
    overlap = _union_overlap_ms(segment.start_ms, segment.end_ms, expanded)
    return overlap < MIN_VAD_OVERLAP_MS and overlap / duration < MIN_VAD_COVERAGE


def _is_post_silence_repeat(
    segment: TranscriptSegment,
    previous: TranscriptSegment | None,
    energy_intervals: Sequence[EnergyInterval],
) -> bool:
    if previous is None:
        return False
    gap_start = previous.end_ms
    gap_end = segment.start_ms
    if (
        gap_end - gap_start <= SILENCE_GAP_MS
        or not normalized_texts_are_similar(
            normalize_transcript_text(segment.text),
            normalize_transcript_text(previous.text),
        )
    ):
        return False
    silent_intervals = [
        interval
        for interval in energy_intervals
        if not interval.has_signal
        and _overlap_ms(gap_start, gap_end, interval.start_ms, interval.end_ms) > 0
    ]
    return bool(silent_intervals) and (
        _union_overlap_ms(gap_start, gap_end, silent_intervals) / (gap_end - gap_start)
        >= SILENCE_ENERGY_COVERAGE
    )


def _has_implausible_speech_rate(
    segment: TranscriptSegment, speech_intervals: Sequence[TimeInterval]
) -> bool:
    duration = segment.end_ms - segment.start_ms
    if duration < MIN_SPEECH_DURATION_MS:
        return False
    effective_speech_ms = _union_overlap_ms(
        segment.start_ms, segment.end_ms, speech_intervals
    )
    if effective_speech_ms <= 0:
        return False
    characters, words = _speech_units(segment.text)
    seconds = effective_speech_ms / 1_000
    return characters / seconds > MAX_CHARS_PER_SECOND or words / seconds > MAX_WORDS_PER_SECOND


def _speech_units(text: str) -> tuple[int, int]:
    without_punctuation = "".join(
        character
        for character in unicodedata.normalize("NFKC", text)
        if not unicodedata.category(character).startswith("P")
    )
    words = len(without_punctuation.split())
    compact = "".join(without_punctuation.split())
    has_cjk = any("\u4e00" <= character <= "\u9fff" for character in compact)
    return (len(compact) if has_cjk else 0, words)


def adjacent_phrase_repetitions(text: str) -> int:
    """Return the repetition count capped at three for the complete safe text."""
    maximum = 1 if text else 0
    for start in range(len(text)):
        for length in range(8, (len(text) - start) // 2 + 1):
            phrase = text[start : start + length]
            if not text.startswith(phrase, start + length):
                continue
            maximum = 2
            if text.startswith(phrase, start + 2 * length):
                return 3
    return maximum


def _union_overlap_ms(
    start_ms: int, end_ms: int, intervals: Sequence[TimeInterval]
) -> int:
    intersections = sorted(
        (
            max(start_ms, interval.start_ms),
            min(end_ms, interval.end_ms),
        )
        for interval in intervals
        if _overlap_ms(start_ms, end_ms, interval.start_ms, interval.end_ms) > 0
    )
    if not intersections:
        return 0
    total = 0
    merged_start, merged_end = intersections[0]
    for interval_start, interval_end in intersections[1:]:
        if interval_start <= merged_end:
            merged_end = max(merged_end, interval_end)
        else:
            total += merged_end - merged_start
            merged_start, merged_end = interval_start, interval_end
    return total + merged_end - merged_start


def _overlap_ms(
    first_start: int, first_end: int, second_start: int, second_end: int
) -> int:
    return max(0, min(first_end, second_end) - max(first_start, second_start))


def _rejected(segment_index: int, reason: str) -> RiskDecision:
    return RiskDecision(segment_index, "REJECTED", reason, False, 0.0)


def _high_risk(segment_index: int, reason: str) -> RiskDecision:
    return RiskDecision(segment_index, "HIGH_RISK_PENDING", reason, False, 0.0)


def _medium_risk(segment_index: int, reason: str) -> RiskDecision:
    return RiskDecision(segment_index, None, reason, True, MEDIUM_RISK_WEIGHT)


def _normal(segment_index: int) -> RiskDecision:
    return RiskDecision(segment_index, None, None, True, 1.0)
