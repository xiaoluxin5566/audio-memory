from dataclasses import replace
import hashlib
import json

import pytest

from audio_memory.transcription.compact import (
    CompactBatch,
    CompactCheckpoint,
    CompactEntry,
    LocalFastParameters,
    SourceRange,
    build_compact_batches,
    normalize_source_ranges,
)


def test_local_fast_parameters_are_the_frozen_v01_values() -> None:
    parameters = LocalFastParameters()

    assert parameters.canonical_dict() == {
        "audio_channels": 1,
        "condition_on_previous_text": False,
        "duplicate_overlap_ratio": 0.3,
        "first_batch_min_speech_ms": 120_000,
        "first_batch_target_speech_ms": 180_000,
        "forced_split_overlap_ms": 1_500,
        "language_lock_code": "zh",
        "language_lock_confidence": 0.9,
        "later_batch_min_speech_ms": 600_000,
        "later_batch_target_speech_ms": 900_000,
        "mapping_tolerance_ms": 300,
        "max_batch_speech_ms": 1_200_000,
        "max_prepared_wavs": 2,
        "max_speech_ms": 1_800_000,
        "min_silence_ms": 250,
        "min_speech_ms": 250,
        "sample_rate_hz": 16_000,
        "secondary_whisper_budget": 0,
        "separator_ms": 500,
        "speech_padding_ms": 500,
        "temperature": 0.0,
        "vad_buffer_seconds": 60,
        "vad_threshold": 0.2,
        "whisper_model_id": "mlx-community/whisper-large-v3-turbo",
        "whisper_workers": 1,
        "word_timestamps": False,
    }


def test_parameter_fingerprint_is_sha256_of_canonical_json_and_changes_with_one_value() -> None:
    parameters = LocalFastParameters()
    payload = json.dumps(
        parameters.canonical_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert parameters.fingerprint() == hashlib.sha256(payload).hexdigest()
    assert replace(parameters, mapping_tolerance_ms=301).fingerprint() != parameters.fingerprint()


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [(-1, 10), (0, 0), (10, 10), (11, 10)],
)
def test_source_range_rejects_invalid_or_non_increasing_time(start_ms: int, end_ms: int) -> None:
    with pytest.raises(ValueError, match="increasing"):
        SourceRange(start_ms, end_ms)


def test_source_range_is_immutable() -> None:
    source_range = SourceRange(10, 20)

    with pytest.raises(AttributeError):
        source_range.start_ms = 11  # type: ignore[misc]


@pytest.mark.parametrize(
    ("ranges", "duration_ms", "padding_ms", "expected"),
    [
        ([SourceRange(5_000, 6_000), SourceRange(1_000, 2_000)], 10_000, 0, ((1_000, 2_000), (5_000, 6_000))),
        ([SourceRange(1_000, 2_000)], 10_000, 500, ((500, 2_500),)),
        ([SourceRange(100, 900), SourceRange(9_500, 11_000)], 10_000, 500, ((0, 1_400), (9_000, 10_000))),
        ([SourceRange(1_000, 2_000), SourceRange(2_200, 3_000)], 10_000, 200, ((800, 3_200),)),
        ([SourceRange(1_000, 2_000), SourceRange(2_000, 3_000)], 10_000, 0, ((1_000, 3_000),)),
        ([SourceRange(1_000, 2_000), SourceRange(2_001, 3_000)], 10_000, 0, ((1_000, 2_000), (2_001, 3_000))),
        ([SourceRange(1_000, 2_000), SourceRange(8_000, 9_000)], 10_000, 500, ((500, 2_500), (7_500, 9_500))),
        ([SourceRange(11_000, 12_000)], 10_000, 0, ()),
    ],
)
def test_normalize_source_ranges(
    ranges: list[SourceRange],
    duration_ms: int,
    padding_ms: int,
    expected: tuple[tuple[int, int], ...],
) -> None:
    normalized = normalize_source_ranges(
        ranges, duration_ms=duration_ms, padding_ms=padding_ms
    )

    assert tuple((item.start_ms, item.end_ms) for item in normalized) == expected


@pytest.mark.parametrize(("duration_ms", "padding_ms"), [(-1, 0), (10, -1)])
def test_normalize_source_ranges_rejects_invalid_bounds(
    duration_ms: int, padding_ms: int
) -> None:
    with pytest.raises(ValueError):
        normalize_source_ranges([], duration_ms=duration_ms, padding_ms=padding_ms)


def _ranges(*pairs: tuple[int, int]) -> tuple[SourceRange, ...]:
    return tuple(SourceRange(*pair) for pair in pairs)


def test_compact_builder_makes_a_small_first_batch_then_larger_later_batches() -> None:
    batches = build_compact_batches(
        _ranges(
            (0, 100_000),
            (101_000, 200_000),
            (201_000, 701_000),
            (702_000, 1_202_000),
            (1_203_000, 1_703_000),
        ),
        LocalFastParameters(),
    )

    assert [item.speech_ms for item in batches] == [199_000, 1_000_000, 500_000]
    assert batches[0].speech_ms >= 120_000
    assert all(item.speech_ms <= 1_200_000 for item in batches)


def test_final_undersized_batch_merges_when_previous_stays_bounded() -> None:
    batches = build_compact_batches(
        _ranges((0, 700_000), (701_000, 1_201_000)),
        LocalFastParameters(first_batch_target_speech_ms=700_000),
    )

    assert len(batches) == 1
    assert batches[0].speech_ms == 1_200_000


def test_compact_entries_insert_only_synthetic_silence_between_distant_sources() -> None:
    [batch] = build_compact_batches(
        _ranges((0, 60_000), (600_000, 660_000)),
        LocalFastParameters(),
    )

    assert [(entry.kind, entry.compact_start_ms, entry.compact_end_ms) for entry in batch.entries] == [
        ("source", 0, 60_000),
        ("separator", 60_000, 60_500),
        ("source", 60_500, 120_500),
    ]
    assert batch.speech_ms == 120_000
    assert batch.compact_ms == 120_500


def test_source_longer_than_max_is_split_with_overlap_and_midpoint_ownership() -> None:
    batches = build_compact_batches(
        _ranges((0, 1_800_000)), LocalFastParameters()
    )

    assert len(batches) == 2
    first = next(entry for entry in batches[0].entries if entry.kind == "source")
    second = next(entry for entry in batches[1].entries if entry.kind == "source")
    assert (first.source_start_ms, first.source_end_ms) == (0, 1_200_000)
    assert (second.source_start_ms, second.source_end_ms) == (1_198_500, 1_800_000)
    assert first.ownership_end_ms == 1_199_250
    assert second.ownership_start_ms == 1_199_250
    assert all(batch.forced_split for batch in batches)


def test_compact_builder_empty_input_and_deterministic_round_trip() -> None:
    assert build_compact_batches((), LocalFastParameters()) == ()
    [batch] = build_compact_batches(
        _ranges((0, 120_000), (121_000, 181_000)), LocalFastParameters()
    )

    restored = CompactBatch.from_dict(batch.to_dict())
    assert restored == batch
    assert restored.parameter_fingerprint == LocalFastParameters().fingerprint()
    assert isinstance(restored.entries[0], CompactEntry)


def test_separator_has_no_source_or_ownership_mapping() -> None:
    separator = CompactEntry.separator(10, 510)

    assert separator.source_start_ms is None
    assert separator.source_end_ms is None
    assert separator.ownership_start_ms is None
    assert separator.ownership_end_ms is None


def test_compact_checkpoint_round_trip_and_fingerprint_fence() -> None:
    parameters = LocalFastParameters()
    checkpoint = CompactCheckpoint(
        parameter_fingerprint=parameters.fingerprint(),
        last_completed_batch=3,
        next_segment_index=40_000,
        language_lock="zh",
    )

    restored = CompactCheckpoint.from_json(checkpoint.to_json(), parameters)
    assert restored == checkpoint
    with pytest.raises(ValueError, match="fingerprint"):
        CompactCheckpoint.from_json(
            checkpoint.to_json(), replace(parameters, mapping_tolerance_ms=301)
        )


def test_empty_checkpoint_starts_before_first_batch() -> None:
    checkpoint = CompactCheckpoint.from_json("{}", LocalFastParameters())
    assert checkpoint.last_completed_batch == -1
    assert checkpoint.next_segment_index == 0
    assert checkpoint.language_lock is None
