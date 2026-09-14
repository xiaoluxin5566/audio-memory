from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from audio_memory.analysis.beta8_event_index import (
    EventIndexCoverageError,
    normalize_event_index_draft,
    validate_event_index,
)
from audio_memory.prompts.beta8_event_index_schema import (
    Beta8EventIndex,
    Beta8EventIndexDraft,
)


def fixture_transcript() -> list[dict[str, object]]:
    return [
        {"file_id": "file-a", "segment_id": "seg-20", "text": "会议开始"},
        {"file_id": "file-a", "segment_id": "seg-1", "text": "确认范围"},
        {"file_id": "file-a", "segment_id": "seg-9", "text": "播客开场"},
        {"file_id": "file-b", "segment_id": "seg-p1", "text": "跨文件续会"},
        {"file_id": "file-b", "segment_id": "seg-p2", "text": "播客继续"},
    ]


def valid_index_payload() -> dict[str, object]:
    return {
        "input_complete": True,
        "input_error": None,
        "activity_sessions": [
            {
                "unit_id": "work-across-files",
                "activity_kind": "conversation",
                "communication_purpose": "work",
                "participation_mode": "user_present_live_interaction",
                "start_boundary": "contact_started",
                "end_boundary": "contact_ended",
                "ranges": [
                    {
                        "source_file": "file-a",
                        "start_segment_id": "seg-20",
                        "end_segment_id": "seg-1",
                    },
                    {
                        "source_file": "file-b",
                        "start_segment_id": "seg-p1",
                        "end_segment_id": "seg-p1",
                    },
                ],
                "subject": "项目沟通",
                "expression_mode": "user_experience",
                "description": "一次工作沟通的连续片段。",
            },
            {
                "unit_id": "podcast",
                "activity_kind": "content_playback",
                "communication_purpose": "not_applicable",
                "participation_mode": "recorded_or_broadcast_content",
                "start_boundary": "activity_started",
                "end_boundary": "activity_ended",
                "ranges": [
                    {
                        "source_file": "file-a",
                        "start_segment_id": "seg-9",
                        "end_segment_id": "seg-9",
                    },
                    {
                        "source_file": "file-b",
                        "start_segment_id": "seg-p2",
                        "end_segment_id": "seg-p2",
                    },
                ],
                "subject": "播客播放",
                "expression_mode": "media_playback",
                "description": "播客播放的连续片段。",
            },
        ],
        "excluded_ranges": [],
    }


def index(payload: dict[str, object] | None = None) -> Beta8EventIndex:
    return Beta8EventIndex.model_validate(payload or valid_index_payload())


def test_event_index_covers_every_segment_exactly_once_in_transcript_order() -> None:
    validate_event_index(index(), fixture_transcript())


def test_event_index_accepts_explicit_exclusion_as_coverage() -> None:
    payload = valid_index_payload()
    payload["activity_sessions"].pop()  # type: ignore[index]
    payload["excluded_ranges"] = [
        {
            "range": {
                "source_file": "file-a",
                "start_segment_id": "seg-9",
                "end_segment_id": "seg-9",
            },
            "reason": "duplicate",
        },
        {
            "range": {
                "source_file": "file-b",
                "start_segment_id": "seg-p2",
                "end_segment_id": "seg-p2",
            },
            "reason": "noise",
        },
    ]

    validate_event_index(index(payload), fixture_transcript())


def test_event_index_rejects_ambiguous_file_name_alias() -> None:
    transcript = [
        {"file_id": "file-a", "file_name": "daily.mp3", "segment_id": "seg-a"},
        {"file_id": "file-b", "file_name": "daily.mp3", "segment_id": "seg-b"},
    ]
    payload = valid_index_payload()
    payload["activity_sessions"] = [
        {
            "unit_id": "ambiguous-name",
            "activity_kind": "other",
            "communication_purpose": "not_applicable",
            "participation_mode": "unknown",
            "start_boundary": "activity_started",
            "end_boundary": "activity_ended",
            "ranges": [{
                "source_file": "daily.mp3",
                "start_segment_id": "seg-a",
                "end_segment_id": "seg-a",
            }],
            "subject": "同名文件",
            "expression_mode": "unknown",
            "description": "用于验证文件名歧义。",
        },
        {
            "unit_id": "other-file",
            "activity_kind": "other",
            "communication_purpose": "not_applicable",
            "participation_mode": "unknown",
            "start_boundary": "activity_started",
            "end_boundary": "activity_ended",
            "ranges": [{
                "source_file": "file-b",
                "start_segment_id": "seg-b",
                "end_segment_id": "seg-b",
            }],
            "subject": "另一份同名文件",
            "expression_mode": "unknown",
            "description": "用于完成测试输入。",
        },
    ]

    with pytest.raises(EventIndexCoverageError, match="ambiguous source file: daily.mp3"):
        validate_event_index(index(payload), transcript)


def test_event_index_ignores_unreliable_segments_and_accepts_unique_file_name_alias() -> None:
    transcript = [
        {
            "file_id": "stable-file-id",
            "file_name": "daily.mp3",
            "segment_id": "reliable-segment",
        },
        {
            "file_id": "stable-file-id",
            "file_name": "daily.mp3",
            "segment_id": "unreliable-segment",
            "is_reliable": False,
        },
    ]
    payload = valid_index_payload()
    payload["activity_sessions"] = [
        {
            "unit_id": "unique-name-alias",
            "activity_kind": "other",
            "communication_purpose": "not_applicable",
            "participation_mode": "unknown",
            "start_boundary": "activity_started",
            "end_boundary": "activity_ended",
            "ranges": [{
                "source_file": "daily.mp3",
                "start_segment_id": "reliable-segment",
                "end_segment_id": "reliable-segment",
            }],
            "subject": "可靠片段",
            "expression_mode": "unknown",
            "description": "唯一文件名可作为来源别名。",
        },
    ]

    validate_event_index(index(payload), transcript)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["activity_sessions"].pop(),  # type: ignore[index]
            "coverage gap",
        ),
        (
            lambda payload: payload["activity_sessions"][1]["ranges"].append(  # type: ignore[index]
                {
                    "source_file": "file-a",
                    "start_segment_id": "seg-1",
                    "end_segment_id": "seg-9",
                }
            ),
            "overlap",
        ),
        (
            lambda payload: payload["activity_sessions"][0]["ranges"].__setitem__(  # type: ignore[index]
                0,
                {
                    "source_file": "unknown-file",
                    "start_segment_id": "seg-20",
                    "end_segment_id": "seg-1",
                },
            ),
            "unknown source file",
        ),
        (
            lambda payload: payload["activity_sessions"][0]["ranges"].__setitem__(  # type: ignore[index]
                0,
                {
                    "source_file": "file-a",
                    "start_segment_id": "unknown-segment",
                    "end_segment_id": "seg-1",
                },
            ),
            "unknown segment ID",
        ),
        (
            lambda payload: payload["activity_sessions"][0]["ranges"].__setitem__(  # type: ignore[index]
                0,
                {
                    "source_file": "file-a",
                    "start_segment_id": "seg-9",
                    "end_segment_id": "seg-20",
                },
            ),
            "reversed range",
        ),
        (
            lambda payload: payload["activity_sessions"].append(  # type: ignore[index]
                deepcopy(payload["activity_sessions"][0])  # type: ignore[index]
            ),
            "duplicate unit_id",
        ),
    ],
)
def test_event_index_rejects_invalid_file_ranges_and_structure(mutate, message: str) -> None:
    payload = valid_index_payload()
    mutate(payload)

    with pytest.raises(EventIndexCoverageError, match=message):
        validate_event_index(index(payload), fixture_transcript())


@pytest.mark.parametrize(
    ("input_complete", "input_error", "message"),
    [
        (True, "partial response", "input_error must be null"),
        (False, None, "input_error is required"),
        (False, "partial response", "input is incomplete"),
    ],
)
def test_event_index_rejects_incomplete_or_inconsistent_input(
    input_complete: bool, input_error: str | None, message: str
) -> None:
    payload = valid_index_payload()
    payload["input_complete"] = input_complete
    payload["input_error"] = input_error

    with pytest.raises(EventIndexCoverageError, match=message):
        validate_event_index(index(payload), fixture_transcript())


def two_layer_draft() -> Beta8EventIndexDraft:
    return Beta8EventIndexDraft.model_validate(
        {
            "input_complete": True,
            "input_error": None,
            "primary_sessions": [
                {
                    "session_id": "session-a",
                    "activity_kind": "conversation",
                    "communication_purpose": "non_work",
                    "participation_mode": "user_present_live_interaction",
                    "start_boundary": "input_start",
                    "start_boundary_evidence_segment_ids": ["seg-1"],
                    "end_boundary": "activity_ended",
                    "end_boundary_evidence_segment_ids": ["seg-2"],
                    "subject": "first session",
                    "description": "The first session covers two segments.",
                },
                {
                    "session_id": "session-b",
                    "activity_kind": "solo_speech_or_activity",
                    "communication_purpose": "not_applicable",
                    "participation_mode": "solo_user_activity",
                    "start_boundary": "activity_started",
                    "start_boundary_evidence_segment_ids": ["seg-4"],
                    "end_boundary": "input_end",
                    "end_boundary_evidence_segment_ids": ["seg-4"],
                    "subject": "second session",
                    "description": "The second session covers the final segment.",
                },
            ],
            "embedded_events": [],
            "file_timelines": [
                {
                    "source_file": "file-a",
                    "blocks": [
                        {
                            "start_segment_id": "seg-1",
                            "end_segment_id": "seg-2",
                            "disposition": "session",
                            "session_id": "session-a",
                        },
                        {
                            "start_segment_id": "seg-3",
                            "end_segment_id": "seg-3",
                            "disposition": "excluded",
                            "excluded_reason": "noise",
                        },
                        {
                            "start_segment_id": "seg-4",
                            "end_segment_id": "seg-4",
                            "disposition": "session",
                            "session_id": "session-b",
                        },
                    ],
                }
            ],
        }
    )


def test_normalizer_preserves_explicit_exclusive_ranges() -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": f"seg-{index}", "text": "text"}
        for index in range(1, 5)
    ]

    normalized = normalize_event_index_draft(two_layer_draft(), transcript)

    assert [
        (
            item.range.start_segment_id,
            item.range.end_segment_id,
            item.disposition,
            item.session_id,
            item.excluded_reason,
        )
        for item in normalized.coverage_ranges
    ] == [
        ("seg-1", "seg-2", "session", "session-a", None),
        ("seg-3", "seg-3", "excluded", None, "noise"),
        ("seg-4", "seg-4", "session", "session-b", None),
    ]
    assert [session.session_id for session in normalized.primary_sessions] == [
        "session-a",
        "session-b",
    ]


def draft_with_embedded_range(start: str, end: str) -> Beta8EventIndexDraft:
    payload = two_layer_draft().model_dump(mode="json")
    payload["embedded_events"] = [
        {
            "event_id": "embedded-media",
            "parent_session_id": "session-a",
            "event_kind": "content_playback",
            "expression_mode": "media_playback",
            "subject": "embedded media",
            "description": "Media played during the primary session.",
            "evidence_ranges": [
                {
                    "source_file": "file-a",
                    "start_segment_id": start,
                    "end_segment_id": end,
                }
            ],
        },
        {
            "event_id": "embedded-commentary",
            "parent_session_id": "session-a",
            "event_kind": "user_commentary",
            "expression_mode": "user_experience",
            "subject": "embedded commentary",
            "description": "The user commented on the media.",
            "evidence_ranges": [
                {
                    "source_file": "file-a",
                    "start_segment_id": start,
                    "end_segment_id": end,
                }
            ],
        },
    ]
    return Beta8EventIndexDraft.model_validate(payload)


def test_normalizer_allows_embedded_evidence_overlap_inside_parent() -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": f"seg-{index}", "text": "text"}
        for index in range(1, 5)
    ]

    normalized = normalize_event_index_draft(
        draft_with_embedded_range("seg-2", "seg-2"), transcript
    )

    assert [event.event_id for event in normalized.embedded_events] == [
        "embedded-media",
        "embedded-commentary",
    ]


def test_normalizer_reparents_embedded_evidence_when_timeline_owner_is_unique() -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": f"seg-{index}", "text": "text"}
        for index in range(1, 5)
    ]

    normalized = normalize_event_index_draft(
        draft_with_embedded_range("seg-4", "seg-4"), transcript
    )

    assert {event.parent_session_id for event in normalized.embedded_events} == {
        "session-b"
    }


def test_normalizer_rejects_embedded_evidence_spanning_multiple_timeline_owners() -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": f"seg-{index}", "text": "text"}
        for index in range(1, 5)
    ]

    with pytest.raises(EventIndexCoverageError, match="outside one primary session"):
        normalize_event_index_draft(
            draft_with_embedded_range("seg-2", "seg-4"), transcript
        )


@pytest.mark.parametrize("evidence_id", ["missing-segment", "seg-4"])
def test_primary_session_boundary_evidence_must_exist_inside_that_session(
    evidence_id: str,
) -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": f"seg-{index}", "text": "text"}
        for index in range(1, 5)
    ]
    payload = two_layer_draft().model_dump(mode="json")
    payload["primary_sessions"][0]["end_boundary_evidence_segment_ids"] = [
        evidence_id
    ]

    with pytest.raises(
        EventIndexCoverageError,
        match="boundary evidence is unknown|boundary evidence is outside primary session",
    ):
        normalize_event_index_draft(
            Beta8EventIndexDraft.model_validate(payload), transcript
        )


@pytest.mark.parametrize("session_number,boundary,evidence", [(0, "end", "seg-11"), (1, "start", "seg-10")])
def test_boundary_context_does_not_expand_ownership(session_number, boundary, evidence):
    transcript = [{"file_id": "file-a", "segment_id": f"seg-{i}", "text": "text"} for i in range(1, 21)]
    payload = adjacent_conversation_draft(distant_evidence=False).model_dump(mode="json")
    payload["primary_sessions"][session_number][f"{boundary}_boundary_evidence_segment_ids"].append(evidence)
    result = normalize_event_index_draft(Beta8EventIndexDraft.model_validate(payload), transcript)
    assert result.primary_sessions[0].ranges[0].end_segment_id == "seg-10"
    assert result.primary_sessions[1].ranges[0].start_segment_id == "seg-11"
    assert evidence in getattr(result.primary_sessions[session_number], f"{boundary}_boundary_evidence_segment_ids")


@pytest.mark.parametrize("session_number,boundary,evidence", [(0, "end", "seg-12"), (1, "start", "seg-9"), (0, "start", "seg-11"), (1, "end", "seg-10")])
def test_boundary_context_rejects_remote_or_wrong_direction(session_number, boundary, evidence):
    transcript = [{"file_id": "file-a", "segment_id": f"seg-{i}", "text": "text"} for i in range(1, 21)]
    payload = adjacent_conversation_draft(distant_evidence=False).model_dump(mode="json")
    payload["primary_sessions"][session_number][f"{boundary}_boundary_evidence_segment_ids"] = [evidence]
    with pytest.raises(EventIndexCoverageError, match="outside primary session"):
        normalize_event_index_draft(Beta8EventIndexDraft.model_validate(payload), transcript)


def test_boundary_context_does_not_infer_adjacency_across_files():
    transcript = [{"file_id": "file-a" if i <= 10 else "file-b", "segment_id": f"seg-{i}", "text": "text"} for i in range(1, 21)]
    payload = adjacent_conversation_draft(distant_evidence=False).model_dump(mode="json")
    second_block = payload["file_timelines"][0]["blocks"].pop()
    payload["file_timelines"].append({"source_file": "file-b", "blocks": [second_block]})
    payload["primary_sessions"][0]["end_boundary_evidence_segment_ids"] = ["seg-10", "seg-11"]
    with pytest.raises(EventIndexCoverageError, match="outside primary session"):
        normalize_event_index_draft(Beta8EventIndexDraft.model_validate(payload), transcript)


def adjacent_conversation_draft(*, distant_evidence: bool) -> Beta8EventIndexDraft:
    payload = two_layer_draft().model_dump(mode="json")
    second = payload["primary_sessions"][1]
    second.update(
        {
            "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "contact_started",
            "end_boundary": "input_end",
            "subject": "second call",
        }
    )
    payload["file_timelines"][0]["blocks"] = [
        {
            "start_segment_id": "seg-1",
            "end_segment_id": "seg-10",
            "disposition": "session",
            "session_id": "session-a",
        },
        {
            "start_segment_id": "seg-11",
            "end_segment_id": "seg-20",
            "disposition": "session",
            "session_id": "session-b",
        },
    ]
    payload["primary_sessions"][0]["end_boundary_evidence_segment_ids"] = [
        "seg-1" if distant_evidence else "seg-10"
    ]
    second["start_boundary_evidence_segment_ids"] = [
        "seg-20" if distant_evidence else "seg-11"
    ]
    second["end_boundary_evidence_segment_ids"] = ["seg-20"]
    return Beta8EventIndexDraft.model_validate(payload)


def test_normalizer_accepts_adjacent_calls_with_nearby_boundary_evidence() -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": f"seg-{index}", "text": "text"}
        for index in range(1, 21)
    ]

    normalized = normalize_event_index_draft(
        adjacent_conversation_draft(distant_evidence=False), transcript
    )

    assert normalized.work_communication_session_ids == ("session-b",)


def test_normalizer_leaves_distant_boundary_semantics_to_review() -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": f"seg-{index}", "text": "text"}
        for index in range(1, 21)
    ]

    result = normalize_event_index_draft(
        adjacent_conversation_draft(distant_evidence=True), transcript
    )
    assert len(result.primary_sessions) == 2


@pytest.mark.parametrize("boundary", ["start", "end"])
@pytest.mark.parametrize("activity_kind,participation,purpose", [
    ("solo_speech_or_activity", "solo_user_activity", "not_applicable"),
    ("content_playback", "recorded_or_broadcast_content", "not_applicable"),
    ("conversation", "user_present_live_interaction", "non_work"),
])
def test_last_block_cannot_swallow_tail_beyond_explicit_end(
    boundary, activity_kind, participation, purpose,
) -> None:
    transcript = [{"file_id": "file-a", "segment_id": f"seg-{i}", "text": "text"}
                  for i in range(1, 41)]
    payload = two_layer_draft().model_dump(mode="json")
    session = payload["primary_sessions"][1]
    session.update(activity_kind=activity_kind, participation_mode=participation,
                   communication_purpose=purpose,
                   start_boundary_evidence_segment_ids=["seg-4"],
                   end_boundary_evidence_segment_ids=["seg-40"])
    session[f"{boundary}_boundary_evidence_segment_ids"] = [
        "seg-40" if boundary == "start" else "seg-4"
    ]
    with pytest.raises(EventIndexCoverageError, match="gap or overlap"):
        normalize_event_index_draft(Beta8EventIndexDraft.model_validate(payload), transcript)


def test_last_block_accepts_evidence_at_actual_coverage_edges() -> None:
    transcript = [{"file_id": "file-a", "segment_id": f"seg-{i}", "text": "text"}
                  for i in range(1, 41)]
    payload = two_layer_draft().model_dump(mode="json")
    payload["primary_sessions"][1]["end_boundary_evidence_segment_ids"] = ["seg-40"]
    payload["file_timelines"][0]["blocks"][-1]["end_segment_id"] = "seg-40"
    result = normalize_event_index_draft(Beta8EventIndexDraft.model_validate(payload), transcript)
    assert result.primary_sessions[1].ranges[-1].end_segment_id == "seg-40"


def test_normalizer_records_unreliable_segments_as_system_exclusions() -> None:
    transcript = [
        {"file_id": "file-a", "segment_id": "seg-1", "text": "valid"},
        {
            "file_id": "file-a",
            "segment_id": "seg-2",
            "text": "bad",
            "is_reliable": False,
        },
        {
            "file_id": "file-a",
            "segment_id": "seg-3",
            "text": "bad",
            "is_reliable": False,
        },
        {"file_id": "file-a", "segment_id": "seg-4", "text": "valid"},
    ]
    payload = two_layer_draft().model_dump(mode="json")
    payload["primary_sessions"][0]["end_boundary_evidence_segment_ids"] = ["seg-1"]
    payload["file_timelines"][0]["blocks"] = [
        {
            "start_segment_id": "seg-1",
            "end_segment_id": "seg-1",
            "disposition": "session",
            "session_id": "session-a",
        },
        {
            "start_segment_id": "seg-4",
            "end_segment_id": "seg-4",
            "disposition": "session",
            "session_id": "session-b",
        },
    ]

    normalized = normalize_event_index_draft(
        Beta8EventIndexDraft.model_validate(payload), transcript
    )

    assert [
        (item.range.start_segment_id, item.range.end_segment_id, item.origin)
        for item in normalized.system_excluded_ranges
    ] == [("seg-2", "seg-3", "system")]


_TWO_LAYER_CASES = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures/beta8/two-layer-index-cases.json"
    ).read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize(
    "case", _TWO_LAYER_CASES, ids=lambda case: str(case["case_id"])
)
def test_two_layer_paid_failure_shapes_normalize_without_special_cases(
    case: dict[str, object],
) -> None:
    normalized = normalize_event_index_draft(
        Beta8EventIndexDraft.model_validate(case["draft"]), case["transcript"]
    )
    actual_ranges = []
    for item in normalized.coverage_ranges:
        row = {
            **item.range.model_dump(mode="json"),
            "disposition": item.disposition,
        }
        if item.session_id is not None:
            row["session_id"] = item.session_id
        if item.excluded_reason is not None:
            row["excluded_reason"] = item.excluded_reason
        actual_ranges.append(row)

    assert actual_ranges == case["expected_coverage_ranges"]
    assert list(normalized.work_communication_session_ids) == case[
        "expected_work_session_ids"
    ]


def test_latest_preserved_paid_response_is_rejected_as_the_old_contract() -> None:
    raw_path = (
        Path(__file__).resolve().parents[4]
        / "outputs/beta8-indexed-v1"
        / "run-2c5b6376-e12f-4c58-94bd-8c47968ad654"
        / "quarantine"
        / "event_index-07eb01a5-99c9-4bf6-b14d-1efea9eb73ff.provider-response.json"
    )

    with pytest.raises(ValidationError):
        Beta8EventIndexDraft.model_validate_json(raw_path.read_text(encoding="utf-8"))
