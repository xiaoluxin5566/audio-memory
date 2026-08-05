from __future__ import annotations

import pytest

from audio_memory.transcription.segments import (
    TranscriptSegment,
    ordered_text,
    progress_percent,
)
from audio_memory.transcription.engine import chunk_segment


def test_segment_rejects_invalid_timestamps() -> None:
    with pytest.raises(ValueError):
        TranscriptSegment("file-1", 0, 500, 400, "倒序", [])


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
