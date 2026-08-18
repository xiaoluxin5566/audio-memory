from __future__ import annotations

import pytest

from audio_memory.transcription.segments import (
    TranscriptSegment,
    ordered_text,
    progress_percent,
)
from audio_memory.transcription.engine import (
    chunk_segment,
    shift_whisper_segments,
    valid_chunk_segments,
)


def test_segment_rejects_invalid_timestamps() -> None:
    with pytest.raises(ValueError):
        TranscriptSegment("file-1", 0, 500, 400, "倒序", [])


def test_segment_rejects_unknown_risk_state() -> None:
    with pytest.raises(ValueError, match="Unknown transcript risk state"):
        TranscriptSegment(
            "file-1",
            0,
            0,
            1000,
            "风险状态无效",
            [],
            risk_state="LOW_CONFIDENCE",
        )


@pytest.mark.parametrize(
    ("risk_state", "is_reliable"),
    [
        ("REJECTED", True),
        ("HIGH_RISK_PENDING", True),
        ("POST_EDIT_FAILED", True),
        ("POST_EDIT_PASSED", False),
    ],
)
def test_segment_rejects_risk_states_with_incompatible_reliability(
    risk_state: str, is_reliable: bool
) -> None:
    with pytest.raises(ValueError, match="requires is_reliable"):
        TranscriptSegment(
            "file-1",
            0,
            0,
            1000,
            "风险状态不匹配",
            [],
            risk_state=risk_state,
            is_reliable=is_reliable,
        )


def test_ordered_text_is_stable_across_files_and_segments() -> None:
    segments = [
        TranscriptSegment("b", 1, 100, 200, "第四", []),
        TranscriptSegment("a", 1, 100, 200, "第二", []),
        TranscriptSegment("b", 0, 0, 100, "第三", []),
        TranscriptSegment("a", 0, 0, 100, "第一", []),
    ]

    assert ordered_text(segments, ["a", "b"]) == "第一\n第二\n第三\n第四"


def test_progress_uses_processed_audio_duration() -> None:
    assert progress_percent(processed_ms=15_000, total_ms=60_000) == 25
    assert progress_percent(processed_ms=70_000, total_ms=60_000) == 100
    assert progress_percent(processed_ms=0, total_ms=0) == 0


def test_chunk_segment_has_stable_global_index_and_timestamps() -> None:
    segment = chunk_segment(
        file_id="file-1",
        chunk_index=2,
        chunk_seconds=300,
        local_index=3,
        raw={"start": 1.5, "end": 4.0, "text": " 继续讨论 ", "words": []},
    )

    assert segment.index == 20_003
    assert segment.start_ms == 601_500
    assert segment.end_ms == 604_000
    assert segment.text == " 继续讨论 "


def test_invalid_whisper_segment_does_not_interrupt_remaining_chunk() -> None:
    segments = list(valid_chunk_segments(
        file_id="file-1", chunk_index=0, chunk_seconds=300,
        raw_segments=[
            {"start": 0, "end": 0, "text": ""},
            {"start": 1, "end": 2, "text": "有效内容", "words": []},
        ],
    ))

    assert [segment.text for segment in segments] == ["有效内容"]


def test_physical_subchunk_timestamps_shift_without_mutating_provider_data() -> None:
    source = [{
        "start": 1.5,
        "end": 3.0,
        "text": "继续讨论",
        "words": [{"word": "继续", "start": 1.5, "end": 2.0}],
    }]

    shifted = shift_whisper_segments(source, offset_seconds=300)

    assert shifted[0]["start"] == 301.5
    assert shifted[0]["end"] == 303.0
    assert shifted[0]["words"][0]["start"] == 301.5
    assert source[0]["start"] == 1.5
    assert source[0]["words"][0]["start"] == 1.5
