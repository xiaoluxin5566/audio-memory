import pytest

from audio_memory.transcription.compact import CompactBatch, CompactEntry
from audio_memory.transcription.mapping import (
    MappingRejection,
    MappedSegment,
    map_segment,
    reconcile_mapped_segments,
)


def _batch() -> CompactBatch:
    return CompactBatch(
        index=0,
        entries=(
            CompactEntry(0, 1_000, 10_000, 11_000, "source", 10_000, 11_000),
            CompactEntry.separator(1_000, 1_500),
            CompactEntry(1_500, 2_500, 20_000, 21_000, "source", 20_000, 21_000),
        ),
        speech_ms=2_000,
        compact_ms=2_500,
        forced_split=False,
        parameter_fingerprint="fingerprint",
    )


def _raw(start_ms: int, end_ms: int, text: str = "有效内容") -> dict[str, object]:
    return {"start_ms": start_ms, "end_ms": end_ms, "text": text, "avg_logprob": -0.2, "no_speech_prob": 0.1}


def test_exact_containment_maps_to_source_time() -> None:
    result = map_segment(_batch(), _raw(100, 900))

    assert isinstance(result, MappedSegment)
    assert (result.start_ms, result.end_ms) == (10_100, 10_900)
    assert result.wholly_mapped is True


@pytest.mark.parametrize(("overrun", "accepted"), [(299, True), (300, True), (301, False)])
def test_following_separator_overrun_has_literal_tolerance(overrun: int, accepted: bool) -> None:
    result = map_segment(_batch(), _raw(900, 1_000 + overrun))

    if accepted:
        assert isinstance(result, MappedSegment)
        assert (result.start_ms, result.end_ms) == (10_900, 11_000)
        assert result.wholly_mapped is False
    else:
        assert result == MappingRejection("severe_boundary_overrun")


def test_separator_only_and_crossing_or_touching_next_source_are_rejected() -> None:
    assert map_segment(_batch(), _raw(1_050, 1_200)) == MappingRejection("separator_only")
    assert map_segment(_batch(), _raw(900, 1_501)) == MappingRejection("cross_source_entry")
    assert map_segment(_batch(), _raw(900, 1_500)) == MappingRejection("cross_source_entry")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (_raw(0, 1, "  "), "empty_text"),
        (_raw(-1, 10), "invalid_time"),
        (_raw(10, 10), "invalid_time"),
        (_raw(20, 10), "invalid_time"),
        (_raw(2_500, 2_600), "outside_batch"),
    ],
)
def test_structurally_invalid_segments_return_safe_reason_codes(raw: dict[str, object], reason: str) -> None:
    assert map_segment(_batch(), raw) == MappingRejection(reason)


def test_mapping_clips_exactly_to_source_file_entry() -> None:
    result = map_segment(_batch(), _raw(950, 1_250))

    assert isinstance(result, MappedSegment)
    assert (result.start_ms, result.end_ms) == (10_950, 11_000)


def _mapped(
    start_ms: int,
    end_ms: int,
    text: str,
    *,
    wholly_mapped: bool = True,
    avg_logprob: float | None = -0.5,
    no_speech_prob: float | None = 0.2,
) -> MappedSegment:
    return MappedSegment(0, start_ms, end_ms, text, avg_logprob, no_speech_prob, wholly_mapped)


def test_reconciliation_deduplicates_safe_text_equivalence_at_literal_threshold() -> None:
    first = _mapped(0, 1_000, "明天，提交方案。", avg_logprob=-0.4)
    duplicate = _mapped(700, 1_700, "明天提交方案", avg_logprob=-0.2)

    result = reconcile_mapped_segments([first, duplicate])

    assert result.kept == (duplicate,)
    assert result.rejected == (first,)
    assert result.conflict_count == 0
    below = reconcile_mapped_segments([first, _mapped(701, 1_701, "明天提交方案")])
    assert len(below.kept) == 2


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("3天后提交", "5天后提交"),
        ("明天不提交", "明天提交"),
        ("会议是8月10日", "会议是8月11日"),
    ],
)
def test_protected_number_or_negation_disagreement_keeps_safer_representative(
    left: str, right: str
) -> None:
    safer = _mapped(0, 1_000, left, avg_logprob=-0.1, no_speech_prob=0.05)
    other = _mapped(700, 1_700, right, avg_logprob=-0.8, no_speech_prob=0.4)

    result = reconcile_mapped_segments([other, safer])

    assert result.kept == (safer,)
    assert result.rejected == (other,)
    assert result.conflict_count == 1


def test_conflict_ranking_prefers_wholly_mapped_then_quality_and_completeness() -> None:
    clipped_high_score = _mapped(0, 1_000, "短句", wholly_mapped=False, avg_logprob=-0.01)
    whole = _mapped(700, 1_700, "这是更完整的另一句", wholly_mapped=True, avg_logprob=-0.8)
    result = reconcile_mapped_segments([clipped_high_score, whole])
    assert result.kept == (whole,)

    shorter = _mapped(2_000, 3_000, "方案", avg_logprob=-0.2, no_speech_prob=0.1)
    longer = _mapped(2_700, 3_700, "方案需要明天提交", avg_logprob=-0.2, no_speech_prob=0.1)
    assert reconcile_mapped_segments([shorter, longer]).kept == (longer,)


def test_structurally_invalid_items_are_rejected_without_erasing_valid_neighbor() -> None:
    invalid = _mapped(1_000, 1_000, "错误时间")
    valid = _mapped(900, 1_900, "保留这句")

    result = reconcile_mapped_segments([invalid, valid])

    assert result.kept == (valid,)
    assert result.rejected == (invalid,)
