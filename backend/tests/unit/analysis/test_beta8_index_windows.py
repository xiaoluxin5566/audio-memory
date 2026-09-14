from __future__ import annotations

from copy import deepcopy

import pytest

from audio_memory.analysis.beta8_index_windows import (
    Beta8IndexWindow,
    EventIndexWindowMergeError,
    merge_event_index_windows,
)
from audio_memory.prompts.beta8_event_index_schema import Beta8NormalizedEventIndex


def transcript() -> list[dict[str, object]]:
    return [
        {"file_name": "day.mp3", "segment_id": f"seg-{number}", "text": "text"}
        for number in range(1, 5)
    ]


def session(session_id: str, start: str, end: str) -> dict[str, object]:
    return {
        "session_id": session_id,
        "activity_kind": "conversation",
        "communication_purpose": "work",
        "participation_mode": "user_present_live_interaction",
        "start_boundary": "input_start" if start == "seg-1" else "activity_started",
        "start_boundary_evidence_segment_ids": [start],
        "end_boundary": "input_end" if end == "seg-4" else "activity_ended",
        "end_boundary_evidence_segment_ids": [end],
        "subject": "project meeting",
        "description": "A continuous project meeting.",
        "ranges": [{
            "source_file": "day.mp3",
            "start_segment_id": start,
            "end_segment_id": end,
        }],
    }


def normalized(
    *, session_rows: list[dict[str, object]], coverage: list[tuple[str, str, str]]
) -> Beta8NormalizedEventIndex:
    return Beta8NormalizedEventIndex.model_validate({
        "input_complete": True,
        "input_error": None,
        "primary_sessions": session_rows,
        "embedded_events": [{
            "event_id": "topic",
            "parent_session_id": str(session_rows[0]["session_id"]),
            "event_kind": "discussion_topic",
            "expression_mode": "user_experience",
            "subject": "delivery",
            "description": "The group discusses delivery.",
            "evidence_ranges": [{
                "source_file": "day.mp3",
                "start_segment_id": "seg-2",
                "end_segment_id": "seg-3",
            }],
        }],
        "coverage_ranges": [
            {
                "range": {
                    "source_file": "day.mp3",
                    "start_segment_id": start,
                    "end_segment_id": end,
                },
                "disposition": "session",
                "session_id": owner,
                "excluded_reason": None,
                "origin": "model",
            }
            for start, end, owner in coverage
        ],
        "system_excluded_ranges": [],
    })


def test_merge_clips_context_and_stitches_confirmed_boundary_session() -> None:
    rows = transcript()
    left = normalized(
        session_rows=[session("left-meeting", "seg-1", "seg-3")],
        coverage=[("seg-1", "seg-3", "left-meeting")],
    )
    right = normalized(
        session_rows=[session("right-meeting", "seg-2", "seg-4")],
        coverage=[("seg-2", "seg-4", "right-meeting")],
    )
    right_payload = right.model_dump(mode="json")
    right_payload["primary_sessions"][0]["communication_purpose"] = "non_work"
    right = Beta8NormalizedEventIndex.model_validate(right_payload)

    merged = merge_event_index_windows(
        [
            Beta8IndexWindow("left", left, rows[:3], rows[:2]),
            Beta8IndexWindow("right", right, rows[1:], rows[2:]),
        ],
        rows,
    )

    assert len(merged.primary_sessions) == 1
    assert merged.primary_sessions[0].session_id == "session_001"
    assert merged.primary_sessions[0].communication_purpose == "mixed"
    assert merged.primary_sessions[0].ranges[0].start_segment_id == "seg-1"
    assert merged.primary_sessions[0].ranges[0].end_segment_id == "seg-4"
    assert [event.parent_session_id for event in merged.embedded_events] == [
        "session_001",
        "session_001",
    ]
    assert [
        (event.evidence_ranges[0].start_segment_id,
         event.evidence_ranges[0].end_segment_id)
        for event in merged.embedded_events
    ] == [("seg-2", "seg-2"), ("seg-3", "seg-3")]
    assert [
        (item.range.start_segment_id, item.range.end_segment_id, item.session_id)
        for item in merged.coverage_ranges
    ] == [("seg-1", "seg-4", "session_001")]


def test_merge_rejects_disagreement_about_cross_window_continuity() -> None:
    rows = transcript()
    left = normalized(
        session_rows=[session("left-meeting", "seg-1", "seg-3")],
        coverage=[("seg-1", "seg-3", "left-meeting")],
    )
    right_payload = normalized(
        session_rows=[
            session("right-before", "seg-2", "seg-2"),
            session("right-after", "seg-3", "seg-4"),
        ],
        coverage=[
            ("seg-2", "seg-2", "right-before"),
            ("seg-3", "seg-4", "right-after"),
        ],
    ).model_dump(mode="json")
    right_payload["embedded_events"] = []
    right = Beta8NormalizedEventIndex.model_validate(right_payload)

    with pytest.raises(EventIndexWindowMergeError, match="continuity disagreement"):
        merge_event_index_windows(
            [
                Beta8IndexWindow("left", left, rows[:3], rows[:2]),
                Beta8IndexWindow("right", right, rows[1:], rows[2:]),
            ],
            rows,
        )


def test_merge_keeps_boundary_split_when_both_windows_agree() -> None:
    rows = transcript()
    left = normalized(
        session_rows=[
            session("left-before", "seg-1", "seg-2"),
            session("left-after", "seg-3", "seg-3"),
        ],
        coverage=[
            ("seg-1", "seg-2", "left-before"),
            ("seg-3", "seg-3", "left-after"),
        ],
    ).model_copy(update={"embedded_events": []})
    right = normalized(
        session_rows=[
            session("right-before", "seg-2", "seg-2"),
            session("right-after", "seg-3", "seg-4"),
        ],
        coverage=[
            ("seg-2", "seg-2", "right-before"),
            ("seg-3", "seg-4", "right-after"),
        ],
    ).model_copy(update={"embedded_events": []})

    merged = merge_event_index_windows(
        [
            Beta8IndexWindow("left", left, rows[:3], rows[:2]),
            Beta8IndexWindow("right", right, rows[1:], rows[2:]),
        ],
        rows,
    )

    assert len(merged.primary_sessions) == 2
    assert [item.session_id for item in merged.primary_sessions] == [
        "session_001",
        "session_002",
    ]
