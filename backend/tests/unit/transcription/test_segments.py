from __future__ import annotations

import pytest

from audio_memory.transcription.segments import (
    TranscriptSegment,
    ordered_text,
    progress_percent,
)


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

