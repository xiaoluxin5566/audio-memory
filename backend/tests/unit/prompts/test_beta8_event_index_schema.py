from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from audio_memory.prompts.beta8_event_index_schema import (
    Beta8EventIndex,
    Beta8EventIndexDraft,
    Beta8NormalizedEventIndex,
)


def valid_draft_payload() -> dict[str, object]:
    return {
        "input_complete": True,
        "input_error": None,
        "primary_sessions": [
            {
                "session_id": "session-call",
                "activity_kind": "conversation",
                "communication_purpose": "work",
                "participation_mode": "user_present_live_interaction",
                "start_boundary": "contact_started",
                "start_boundary_evidence_segment_ids": ["seg-1"],
                "end_boundary": "contact_ended",
                "end_boundary_evidence_segment_ids": ["seg-2"],
                "subject": "project call",
                "description": "The user joined a project call.",
            }
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
                        "session_id": "session-call",
                    }
                ],
            }
        ],
    }


def test_timeline_session_block_forbids_excluded_reason() -> None:
    payload = valid_draft_payload()
    block = payload["file_timelines"][0]["blocks"][0]  # type: ignore[index]
    block["excluded_reason"] = "noise"  # type: ignore[index]

    with pytest.raises(ValidationError, match="excluded_reason"):
        Beta8EventIndexDraft.model_validate(payload)


def test_timeline_excluded_block_forbids_session_id() -> None:
    payload = valid_draft_payload()
    payload["file_timelines"][0]["blocks"][0] = {  # type: ignore[index]
        "start_segment_id": "seg-1",
        "disposition": "excluded",
        "excluded_reason": "noise",
        "session_id": "session-call",
    }

    with pytest.raises(ValidationError, match="session_id"):
        Beta8EventIndexDraft.model_validate(payload)


def normalized_index_payload() -> dict[str, object]:
    primary = dict(valid_draft_payload()["primary_sessions"][0])  # type: ignore[index]
    primary["ranges"] = [
        {
            "source_file": "file-a",
            "start_segment_id": "seg-1",
            "end_segment_id": "seg-2",
        }
    ]
    return {
        "input_complete": True,
        "input_error": None,
        "primary_sessions": [primary],
        "embedded_events": [
            {
                "event_id": "embedded-demo",
                "parent_session_id": "session-call",
                "event_kind": "content_playback",
                "expression_mode": "media_playback",
                "subject": "demo",
                "description": "A demo played during the call.",
                "evidence_ranges": [
                    {
                        "source_file": "file-a",
                        "start_segment_id": "seg-2",
                        "end_segment_id": "seg-2",
                    }
                ],
            }
        ],
        "coverage_ranges": [
            {
                "range": {
                    "source_file": "file-a",
                    "start_segment_id": "seg-1",
                    "end_segment_id": "seg-2",
                },
                "disposition": "session",
                "session_id": "session-call",
                "origin": "model",
            }
        ],
        "system_excluded_ranges": [],
    }


def test_normalized_index_routes_embedded_events_but_only_primary_work_sessions() -> None:
    index = Beta8NormalizedEventIndex.model_validate(normalized_index_payload())

    assert index.work_communication_session_ids == ("session-call",)
    assert index.routeable_unit_ids == ("session-call", "embedded-demo")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"input_error": "unexpected"}),
        lambda payload: payload.update({"input_complete": False}),
    ],
)
def test_draft_rejects_inconsistent_completion_state(mutate) -> None:
    payload = valid_draft_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        Beta8EventIndexDraft.model_validate(payload)


def test_incomplete_draft_requires_an_error_and_empty_content() -> None:
    payload = {
        "input_complete": False,
        "input_error": None,
        "primary_sessions": [],
        "embedded_events": [],
        "file_timelines": [],
    }

    with pytest.raises(ValidationError, match="input_error"):
        Beta8EventIndexDraft.model_validate(payload)


def embedded_event_payload(event_id: str = "embedded-demo") -> dict[str, object]:
    return {
        "event_id": event_id,
        "parent_session_id": "session-call",
        "event_kind": "content_playback",
        "expression_mode": "media_playback",
        "subject": "demo",
        "description": "A demo played during the call.",
        "evidence_ranges": [
            {
                "source_file": "file-a",
                "start_segment_id": "seg-1",
                "end_segment_id": "seg-1",
            }
        ],
    }


def test_draft_rejects_more_than_128_primary_sessions() -> None:
    payload = valid_draft_payload()
    template = payload["primary_sessions"][0]  # type: ignore[index]
    payload["primary_sessions"] = [
        {**deepcopy(template), "session_id": f"session-{index}"}
        for index in range(129)
    ]

    with pytest.raises(ValidationError, match="at most 128 items"):
        Beta8EventIndexDraft.model_validate(payload)


def test_draft_rejects_more_than_256_embedded_events() -> None:
    payload = valid_draft_payload()
    payload["embedded_events"] = [
        embedded_event_payload(f"event-{index}") for index in range(257)
    ]

    with pytest.raises(ValidationError, match="at most 256 items"):
        Beta8EventIndexDraft.model_validate(payload)


def test_draft_rejects_per_file_and_global_block_limits() -> None:
    payload = valid_draft_payload()
    block = payload["file_timelines"][0]["blocks"][0]  # type: ignore[index]
    payload["file_timelines"][0]["blocks"] = [  # type: ignore[index]
        {**deepcopy(block), "start_segment_id": f"seg-{index}"}
        for index in range(257)
    ]
    with pytest.raises(ValidationError, match="at most 256 items"):
        Beta8EventIndexDraft.model_validate(payload)

    payload = valid_draft_payload()
    payload["file_timelines"] = [
        {
            "source_file": f"file-{file_index}",
            "blocks": [
                {**deepcopy(block), "start_segment_id": f"seg-{file_index}-{index}"}
                for index in range(171)
            ],
        }
        for file_index in range(3)
    ]
    with pytest.raises(ValidationError, match="more than 512"):
        Beta8EventIndexDraft.model_validate(payload)


def test_draft_rejects_verbose_boundary_evidence_and_embedded_ranges() -> None:
    payload = valid_draft_payload()
    payload["primary_sessions"][0]["start_boundary_evidence_segment_ids"] = [  # type: ignore[index]
        f"seg-{index}" for index in range(9)
    ]
    with pytest.raises(ValidationError, match="at most 8 items"):
        Beta8EventIndexDraft.model_validate(payload)

    payload = valid_draft_payload()
    event = embedded_event_payload()
    event["evidence_ranges"] = [
        deepcopy(event["evidence_ranges"][0])  # type: ignore[index]
        for _ in range(33)
    ]
    payload["embedded_events"] = [event]
    with pytest.raises(ValidationError, match="at most 32 items"):
        Beta8EventIndexDraft.model_validate(payload)


@pytest.mark.parametrize("duplicate_kind", ["session", "event", "cross-layer"])
def test_draft_rejects_duplicate_routeable_ids(duplicate_kind: str) -> None:
    payload = valid_draft_payload()
    if duplicate_kind == "session":
        payload["primary_sessions"] = [
            deepcopy(payload["primary_sessions"][0]),  # type: ignore[index]
            deepcopy(payload["primary_sessions"][0]),  # type: ignore[index]
        ]
    elif duplicate_kind == "event":
        payload["embedded_events"] = [embedded_event_payload(), embedded_event_payload()]
    else:
        payload["embedded_events"] = [embedded_event_payload("session-call")]

    with pytest.raises(ValidationError, match="unique"):
        Beta8EventIndexDraft.model_validate(payload)


def activity_session_payload() -> dict[str, object]:
    return {
        "input_complete": True,
        "input_error": None,
        "activity_sessions": [
            {
                "unit_id": "lunch-conversation",
                "activity_kind": "conversation",
                "communication_purpose": "non_work",
                "participation_mode": "user_present_live_interaction",
                "start_boundary": "contact_started",
                "end_boundary": "contact_ended",
                "ranges": [
                    {
                        "source_file": "file-a",
                        "start_segment_id": "seg-1",
                        "end_segment_id": "seg-9",
                    }
                ],
                "subject": "午餐期间的职业与生活聊天",
                "expression_mode": "mixed",
                "description": "同事间在午餐中聊离职、薪资、年假和职业选择。",
            }
        ],
        "excluded_ranges": [],
    }


def test_event_index_models_a_real_activity_session_separately_from_work_purpose() -> None:
    """Break caught: restoring scene-like kind labels would conflate topic and activity."""
    index = Beta8EventIndex.model_validate(activity_session_payload())

    session = index.activity_sessions[0]
    assert session.activity_kind == "conversation"
    assert session.communication_purpose == "non_work"
    assert session.is_work_communication is False


@pytest.mark.parametrize(
    ("activity_kind", "communication_purpose"),
    [
        ("content_playback", "work"),
        ("conversation", "not_applicable"),
    ],
)
def test_event_index_rejects_incoherent_activity_and_communication_purpose(
    activity_kind: str, communication_purpose: str
) -> None:
    """Break caught: work semantics could otherwise leak onto non-conversation units."""
    payload = activity_session_payload()
    session = payload["activity_sessions"][0]  # type: ignore[index]
    session["activity_kind"] = activity_kind
    session["communication_purpose"] = communication_purpose

    with pytest.raises(ValidationError, match="communication purpose"):
        Beta8EventIndex.model_validate(payload)


def test_mixed_conversation_remains_a_work_communication() -> None:
    """Break caught: a real work phase must not disappear inside a mixed conversation."""
    payload = activity_session_payload()
    session = payload["activity_sessions"][0]  # type: ignore[index]
    session["communication_purpose"] = "mixed"

    index = Beta8EventIndex.model_validate(payload)

    assert index.activity_sessions[0].is_work_communication is True


def test_activity_session_records_participation_and_real_boundary_cues() -> None:
    """Break caught: media dialogue and topic changes must not masquerade as sessions."""
    payload = activity_session_payload()
    session = payload["activity_sessions"][0]  # type: ignore[index]
    session["participation_mode"] = "user_present_live_interaction"
    session["start_boundary"] = "contact_started"
    session["end_boundary"] = "contact_ended"

    index = Beta8EventIndex.model_validate(payload)

    parsed = index.activity_sessions[0]
    assert parsed.participation_mode == "user_present_live_interaction"
    assert parsed.start_boundary == "contact_started"
    assert parsed.end_boundary == "contact_ended"


@pytest.mark.parametrize(
    "updates",
    [
        {
            "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "recorded_or_broadcast_content",
        },
        {
            "activity_kind": "content_playback",
            "communication_purpose": "not_applicable",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "activity_started",
            "end_boundary": "activity_ended",
        },
    ],
)
def test_activity_session_rejects_media_as_live_work(
    updates: dict[str, str],
) -> None:
    """Break caught: media playback cannot masquerade as the user's live work."""
    payload = activity_session_payload()
    session = payload["activity_sessions"][0]  # type: ignore[index]
    session.update(updates)

    with pytest.raises(ValidationError, match="participation"):
        Beta8EventIndex.model_validate(payload)


def test_in_person_conversation_allows_activity_boundaries() -> None:
    """A live in-person exchange may begin naturally, without a phone contact cue."""
    payload = activity_session_payload()
    session = payload["activity_sessions"][0]  # type: ignore[index]
    session.update(
        {
            "communication_purpose": "non_work",
            "start_boundary": "activity_started",
            "end_boundary": "activity_ended",
        }
    )

    parsed = Beta8EventIndex.model_validate(payload)

    assert parsed.activity_sessions[0].start_boundary == "activity_started"
    assert parsed.activity_sessions[0].end_boundary == "activity_ended"


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


def test_event_index_schema_is_thin_and_forbids_value_selection_fields() -> None:
    payload = valid_index_payload()
    payload["activity_sessions"][0]["card_candidate"] = True  # type: ignore[index]

    with pytest.raises(ValidationError, match="card_candidate"):
        Beta8EventIndex.model_validate(payload)


def test_event_index_schema_keeps_cross_file_unit_as_multiple_ranges() -> None:
    index = Beta8EventIndex.model_validate(valid_index_payload())

    assert index.units[0].unit_id == "work-across-files"
    assert [item.source_file for item in index.units[0].ranges] == ["file-a", "file-b"]


def test_event_index_schema_rejects_more_than_128_units() -> None:
    payload = valid_index_payload()
    template = payload["activity_sessions"][0]  # type: ignore[index]
    payload["activity_sessions"] = []
    for item_index in range(129):
        unit = deepcopy(template)
        unit["unit_id"] = f"unit-{item_index}"
        payload["activity_sessions"].append(unit)  # type: ignore[union-attr]

    with pytest.raises(ValidationError, match="at most 128 items"):
        Beta8EventIndex.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject", "主" * 121),
        ("description", "描" * 301),
    ],
)
def test_event_index_schema_rejects_verbose_unit_text(field: str, value: str) -> None:
    payload = valid_index_payload()
    payload["activity_sessions"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValidationError, match="String should have at most"):
        Beta8EventIndex.model_validate(payload)


def test_event_index_schema_rejects_more_than_32_ranges_per_unit() -> None:
    payload = valid_index_payload()
    template = payload["activity_sessions"][0]["ranges"][0]  # type: ignore[index]
    payload["activity_sessions"][0]["ranges"] = [deepcopy(template) for _ in range(33)]  # type: ignore[index]

    with pytest.raises(ValidationError, match="at most 32 items"):
        Beta8EventIndex.model_validate(payload)


def test_event_index_schema_rejects_more_than_128_excluded_ranges() -> None:
    payload = valid_index_payload()
    exclusion = {
        "range": {
            "source_file": "file-a",
            "start_segment_id": "seg-1",
            "end_segment_id": "seg-1",
        },
        "reason": "noise",
    }
    payload["excluded_ranges"] = [deepcopy(exclusion) for _ in range(129)]

    with pytest.raises(ValidationError, match="at most 128 items"):
        Beta8EventIndex.model_validate(payload)
