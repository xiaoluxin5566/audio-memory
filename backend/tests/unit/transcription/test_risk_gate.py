from __future__ import annotations

from dataclasses import fields

from audio_memory.transcription.risk_gate import (
    EnergyInterval,
    TimeInterval,
    classify_segments,
    normalize_transcript_text,
    normalized_similarity,
)
from audio_memory.transcription.segments import TranscriptSegment


def segment(index: int, start_ms: int, end_ms: int, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        file_id="file-1",
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        words=[],
    )


def invalid_segment(start_ms: int, end_ms: int, text: str) -> TranscriptSegment:
    """Construct a persisted-corruption fixture that bypasses constructor checks."""
    value = object.__new__(TranscriptSegment)
    values = {
        "file_id": "file-1",
        "index": 0,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "words": [],
        "risk_state": None,
        "is_reliable": True,
        "reliability_weight": 1.0,
        "risk_reason": None,
        "no_speech_prob": None,
        "avg_logprob": None,
    }
    for field in fields(TranscriptSegment):
        object.__setattr__(value, field.name, values[field.name])
    return value


def test_normalize_transcript_text_unifies_nfkc_punctuation_case_and_numbers() -> None:
    # Removing NFKC, punctuation/space folding, case folding, or numeral parsing
    # would make these semantically identical utterances compare differently.
    assert normalize_transcript_text("ＦＯＯ，会议三点。") == "foo会议3点"
    assert normalize_transcript_text("foo 会议 3 点!") == "foo会议3点"
    assert normalize_transcript_text("第 一千零一 项") == "第1001项"


def test_normalized_similarity_uses_normalized_levenshtein_distance() -> None:
    # Dividing by the wrong length or using a different distance metric would break
    # the fixed 0.90 risk threshold.
    assert normalized_similarity("abcdefghij", "abcdefghiX") == 0.9
    assert normalized_similarity("", "") == 0.0


def test_rejects_segment_without_sufficient_vad_support_after_grace() -> None:
    decisions = classify_segments(
        [segment(0, 0, 2_000, "有文本")],
        [TimeInterval(0, 100)],
        [],
    )

    assert decisions[0].state == "REJECTED"
    assert decisions[0].reason == "no_vad_support"
    assert decisions[0].is_reliable is False
    assert decisions[0].reliability_weight == 0.0


def test_unavailable_vad_downgrades_valid_text_but_keeps_hard_checks_active() -> None:
    decisions = classify_segments(
        [
            segment(0, 0, 1_000, "valid fallback transcript"),
            invalid_segment(500, 500, "invalid fallback timing"),
        ],
        [],
        [],
        owning_window=TimeInterval(0, 1_000),
        vad_available=False,
    )

    assert (decisions[0].state, decisions[0].reason) == (None, "vad_unavailable")
    assert decisions[0].reliability_weight == 0.6
    assert (decisions[1].state, decisions[1].reason) == (
        "REJECTED",
        "invalid_timing",
    )


def test_rejects_blank_and_invalid_timing_segments_from_corrupt_input() -> None:
    decisions = classify_segments(
        [invalid_segment(100, 100, "文本"), invalid_segment(0, 1_000, "   ")],
        [TimeInterval(0, 1_000)],
        [],
    )

    assert [decision.reason for decision in decisions] == ["invalid_timing", "blank_text"]
    assert all(decision.state == "REJECTED" for decision in decisions)


def test_rejects_segments_outside_their_owning_file_window() -> None:
    # Dropping the owning-window check would let otherwise VAD-supported corrupt
    # timestamps cross the file boundary and enter downstream analysis.
    decisions = classify_segments(
        [
            invalid_segment(-100, 500, "head overflow"),
            invalid_segment(800, 1_100, "tail overflow"),
        ],
        [TimeInterval(0, 1_100)],
        [],
        owning_window=TimeInterval(0, 1_000),
    )

    assert [decision.reason for decision in decisions] == [
        "outside_file_window",
        "outside_file_window",
    ]
    assert all(decision.state == "REJECTED" for decision in decisions)


def test_rejects_every_segment_in_an_unresolved_cross_segment_time_conflict() -> None:
    # Removing the cross-segment pass would trust both sides of a boundary merge
    # conflict merely because each segment is individually well formed.
    decisions = classify_segments(
        [
            segment(0, 0, 1_500, "first boundary result"),
            segment(1, 1_000, 2_000, "conflicting boundary result"),
            segment(2, 2_000, 3_000, "clean result"),
        ],
        [TimeInterval(0, 3_000)],
        [],
        owning_window=TimeInterval(0, 3_000),
    )

    assert [decision.reason for decision in decisions] == [
        "timestamp_conflict",
        "timestamp_conflict",
        None,
    ]
    assert [decision.state for decision in decisions] == ["REJECTED", "REJECTED", None]


def test_vad_grace_keeps_segment_when_coverage_reaches_thirty_percent() -> None:
    decisions = classify_segments(
        [segment(0, 0, 1_000, "有文本")],
        [TimeInterval(0, 1)],
        [],
    )

    assert decisions[0].state is None
    assert decisions[0].is_reliable is True
    assert decisions[0].reliability_weight == 1.0


def test_vad_grace_keeps_segment_when_overlap_reaches_half_second() -> None:
    decisions = classify_segments(
        [segment(0, 0, 2_000, "有文本")],
        [TimeInterval(0, 200)],
        [],
    )

    assert decisions[0].state is None
    assert decisions[0].reliability_weight == 1.0


def test_marks_third_similar_segment_within_thirty_seconds_high_risk() -> None:
    decisions = classify_segments(
        [
            segment(0, 0, 1_000, "会议三点"),
            segment(1, 5_000, 6_000, "会议 3 点"),
            segment(2, 10_000, 11_000, "会议三点。"),
        ],
        [TimeInterval(0, 12_000)],
        [],
    )

    assert decisions[2].state == "HIGH_RISK_PENDING"
    assert decisions[2].reason == "repeated_nearby"
    assert decisions[2].is_reliable is False


def test_exact_repeat_detection_survives_the_bounded_approximate_comparison_cap() -> None:
    # Removing the linear exact-text index would let a crowded 30-second window
    # push the first occurrence outside the bounded edit-distance comparison set.
    items = [
        segment(index, index * 100, index * 100 + 100, f"unique token {index}")
        for index in range(260)
    ]
    for index in (0, 257, 259):
        items[index] = segment(
            index,
            index * 100,
            index * 100 + 100,
            "exact repeated anchor",
        )

    decisions = classify_segments(
        items,
        [TimeInterval(0, 26_000)],
        [],
    )

    assert decisions[259].state == "HIGH_RISK_PENDING"
    assert decisions[259].reason == "repeated_nearby"


def test_approximate_repeat_detection_keeps_the_entire_thirty_second_window() -> None:
    items = [
        segment(0, 0, 90, "approximate repeated anchor aa"),
        segment(1, 100, 190, "approximate repeated anchor ab"),
    ]
    items.extend(
        segment(index, index * 100, index * 100 + 90, chr(0x3400 + index))
        for index in range(2, 259)
    )
    items.append(
        segment(259, 25_900, 25_990, "approximate repeated anchor ac")
    )

    decisions = classify_segments(
        items,
        [TimeInterval(0, 26_000)],
        [],
    )

    assert decisions[259].state == "HIGH_RISK_PENDING"
    assert decisions[259].reason == "repeated_nearby"


def test_unprovable_crowded_similarity_window_is_rejected() -> None:
    items = [
        segment(
            index,
            index * 100,
            index * 100 + 90,
            chr(0x3400 + index) + chr(0x5000 + index) + "abcdefghij",
        )
        for index in range(258)
    ]

    decisions = classify_segments(
        items,
        [TimeInterval(0, 26_000)],
        [],
    )

    assert decisions[257].state == "REJECTED"
    assert decisions[257].reason == "similarity_comparison_budget_exhausted"


def test_marks_two_similar_segments_as_medium_risk_only() -> None:
    decisions = classify_segments(
        [segment(0, 0, 1_000, "会议三点"), segment(1, 5_000, 6_000, "会议 3 点")],
        [TimeInterval(0, 6_000)],
        [],
    )

    assert decisions[1].state is None
    assert decisions[1].reason == "light_repetition"
    assert decisions[1].is_reliable is True
    assert decisions[1].reliability_weight == 0.6


def test_marks_three_adjacent_long_phrase_repetitions_high_risk() -> None:
    decisions = classify_segments(
        [segment(0, 0, 2_000, "请明天下午提交预算请明天下午提交预算请明天下午提交预算")],
        [TimeInterval(0, 2_000)],
        [],
    )

    assert decisions[0].state == "HIGH_RISK_PENDING"
    assert decisions[0].reason == "repeated_phrase"


def test_phrase_repetition_after_the_first_512_characters_is_high_risk() -> None:
    prefix = "".join(chr(0x3400 + index) for index in range(520))
    phrase = "风险后缀需要隔离"
    decisions = classify_segments(
        [segment(0, 0, 2_000, prefix + phrase * 3)],
        [TimeInterval(0, 2_000)],
        [],
    )

    assert decisions[0].state == "HIGH_RISK_PENDING"
    assert decisions[0].reason == "repeated_phrase"


def test_text_beyond_safe_full_comparison_limit_is_rejected() -> None:
    oversized = "".join(chr(0x3400 + index) for index in range(2_000))

    decisions = classify_segments(
        [segment(0, 0, 2_000, oversized)],
        [TimeInterval(0, 2_000)],
        [],
    )

    assert decisions[0].state == "REJECTED"
    assert decisions[0].reason == "comparison_text_too_long"


def test_marks_repeat_after_confirmed_noninitial_silence_high_risk() -> None:
    decisions = classify_segments(
        [segment(0, 0, 1_000, "请继续"), segment(1, 12_000, 13_000, "请继续")],
        [TimeInterval(0, 1_000), TimeInterval(12_000, 13_000)],
        [EnergyInterval(1_000, 12_000, has_signal=False)],
    )

    assert decisions[1].state == "HIGH_RISK_PENDING"
    assert decisions[1].reason == "post_silence_repeat"


def test_silence_repeat_allows_up_to_nineteen_percent_signal_energy() -> None:
    decisions = classify_segments(
        [segment(0, 0, 1_000, "请继续"), segment(1, 12_000, 13_000, "请继续")],
        [TimeInterval(0, 1_000), TimeInterval(12_000, 13_000)],
        [
            EnergyInterval(1_000, 6_000, has_signal=False),
            EnergyInterval(5_000, 9_910, has_signal=False),
            EnergyInterval(9_910, 12_000, has_signal=True),
        ],
    )

    assert decisions[1].state == "HIGH_RISK_PENDING"
    assert decisions[1].reason == "post_silence_repeat"


def test_silence_repeat_requires_eighty_percent_energy_coverage() -> None:
    decisions = classify_segments(
        [segment(0, 0, 1_000, "请继续"), segment(1, 12_000, 13_000, "请继续")],
        [TimeInterval(0, 1_000), TimeInterval(12_000, 13_000)],
        [
            EnergyInterval(1_000, 6_000, has_signal=False),
            EnergyInterval(5_000, 9_690, has_signal=False),
            EnergyInterval(9_690, 12_000, has_signal=True),
        ],
    )

    assert decisions[1].state is None
    assert decisions[1].reason == "light_repetition"


def test_rejected_segment_does_not_contribute_to_repeat_history() -> None:
    decisions = classify_segments(
        [
            segment(0, 0, 1_000, "重复文本"),
            segment(1, 5_000, 6_000, "重复文本"),
            segment(2, 40_000, 41_000, "重复文本"),
        ],
        [TimeInterval(5_000, 6_000), TimeInterval(40_000, 41_000)],
        [],
    )

    assert decisions[0].state == "REJECTED"
    assert [(decision.state, decision.reason) for decision in decisions[1:]] == [
        (None, None),
        (None, None),
    ]


def test_does_not_treat_first_segment_as_post_silence_repeat() -> None:
    decisions = classify_segments(
        [segment(0, 15_000, 16_000, "请继续")],
        [TimeInterval(15_000, 16_000)],
        [EnergyInterval(0, 15_000, has_signal=False)],
    )

    assert decisions[0].state is None
    assert decisions[0].reason is None


def test_high_speech_rate_requires_effective_speech_and_light_repetition() -> None:
    decisions = classify_segments(
        [
            segment(0, 0, 2_000, "一二三四五六七八九十一二三四五六七八九十"),
            segment(1, 3_000, 5_000, "一二三四五六七八九十一二三四五六七八九十"),
        ],
        [TimeInterval(0, 2_000), TimeInterval(3_000, 3_600)],
        [],
    )

    assert decisions[1].state == "HIGH_RISK_PENDING"
    assert decisions[1].reason == "implausible_speech_rate"


def test_high_rate_without_repetition_remains_normal() -> None:
    decisions = classify_segments(
        [segment(0, 0, 2_000, "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉")],
        [TimeInterval(0, 2_000)],
        [],
    )

    assert decisions[0].state is None
    assert decisions[0].reason is None
    assert decisions[0].reliability_weight == 1.0


def test_english_speech_rate_uses_words_not_characters() -> None:
    decisions = classify_segments(
        [
            segment(0, 0, 2_000, "supercalifragilistic"),
            segment(1, 3_000, 5_000, "supercalifragilistic"),
        ],
        [TimeInterval(0, 2_000), TimeInterval(3_000, 3_600)],
        [],
    )

    assert decisions[1].state is None
    assert decisions[1].reason == "light_repetition"
