from __future__ import annotations

import pytest

from audio_memory.analysis.windows import (
    AnalysisWindowError,
    build_analysis_windows,
)


def segment(
    segment_id: str,
    file_id: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "file_id": file_id,
        "file_name": f"{file_id}.mp3",
        "recording_started_at": None,
        "local_date": None,
        "timezone": None,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "speaker_id": "unknown",
        "text": f"synthetic {segment_id}",
        "reliability_weight": 1.0,
    }


def test_build_analysis_windows_splits_on_file_and_frozen_gap_boundaries() -> None:
    transcript = [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 45_999, 46_999),
        segment("seg_0_2", "file-a", 91_999, 92_999),
        segment("seg_1_0", "file-b", 0, 1_000),
    ]

    windows = build_analysis_windows(transcript)

    assert [
        [str(item["segment_id"]) for item in window.segments]
        for window in windows
    ] == [["seg_0_0", "seg_0_1"], ["seg_0_2"], ["seg_1_0"]]
    assert [window.window_id for window in windows] == [
        "window_0000",
        "window_0001",
        "window_0002",
    ]


def test_build_analysis_windows_splits_before_span_limit_is_exceeded() -> None:
    transcript = [
        segment(
            f"seg_0_{index}",
            "file-a",
            index * 40_000,
            index * 40_000 + (1 if index == 30 else 1_000),
        )
        for index in range(31)
    ]

    windows = build_analysis_windows(transcript)

    assert [len(window.segments) for window in windows] == [30, 1]
    assert [(window.start_ms, window.end_ms) for window in windows] == [
        (0, 1_161_000),
        (1_200_000, 1_200_001),
    ]


def test_build_analysis_windows_splits_before_401st_segment() -> None:
    transcript = [
        segment(f"seg_0_{index}", "file-a", index * 2_000, index * 2_000 + 1_000)
        for index in range(401)
    ]

    windows = build_analysis_windows(transcript)

    assert [len(window.segments) for window in windows] == [400, 1]
    assert windows[1].segments[0]["segment_id"] == "seg_0_400"


def test_build_analysis_windows_stably_sorts_and_covers_every_segment_once() -> None:
    transcript = [
        segment("seg_b_1", "file-b", 2_000, 3_000),
        segment("seg_a_1", "file-a", 2_000, 3_000),
        segment("seg_b_0", "file-b", 0, 1_000),
        segment("seg_a_0", "file-a", 0, 1_000),
    ]

    windows = build_analysis_windows(transcript)

    flattened = [
        str(item["segment_id"])
        for window in windows
        for item in window.segments
    ]
    assert flattened == ["seg_b_0", "seg_b_1", "seg_a_0", "seg_a_1"]
    assert len(flattened) == len(set(flattened)) == len(transcript)
    assert [(window.file_id, window.start_ms, window.end_ms) for window in windows] == [
        ("file-b", 0, 3_000),
        ("file-a", 0, 3_000),
    ]


@pytest.mark.parametrize(
    "transcript",
    [
        [
            segment("seg_duplicate", "file-a", 0, 1_000),
            segment("seg_duplicate", "file-a", 2_000, 3_000),
        ],
        [segment("seg_invalid", "file-a", 1_000, 1_000)],
        [segment("seg_negative", "file-a", -1, 1_000)],
    ],
)
def test_build_analysis_windows_rejects_invalid_segment_structure(
    transcript: list[dict[str, object]],
) -> None:
    with pytest.raises(AnalysisWindowError):
        build_analysis_windows(transcript)
