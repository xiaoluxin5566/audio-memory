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


def test_rejects_blank_and_invalid_timing_segments_from_corrupt_input() -> None:
    decisions = classify_segments(
        [invalid_segment(100, 100, "文本"), invalid_segment(0, 1_000, "   ")],
        [TimeInterval(0, 1_000)],
        [],
    )

    assert [decision.reason for decision in decisions] == ["invalid_timing", "blank_text"]
    assert all(decision.state == "REJECTED" for decision in decisions)


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
