from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_memory.prompts.event_schema import (
    Event,
    EventMap,
    StructuredTranscriptSegment,
    UserSpeaker,
)


def test_event_map_allows_model_to_omit_server_owned_unassigned_ids() -> None:
    event_map = EventMap.model_validate(
        {
            "user_speaker": {
                "speaker_id": None,
                "confidence": 0,
                "reasoning": "无法判断",
                "evidence_segment_ids": [],
            },
            "events": [],
        }
    )

    assert event_map.unassigned_segment_ids == []


def event(
    event_id: str,
    *,
    segment_id: str = "seg_001",
    parent_event_id: str | None = None,
    event_type: str = "conversation",
    start_ms: int = 0,
    end_ms: int = 1_000,
) -> Event:
    return Event(
        event_id=event_id,
        parent_event_id=parent_event_id,
        event_type=event_type,
        title="讨论一期范围",
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_ids=["speaker_A"],
        user_role="参与者",
        user_role_confidence=0.9,
        factual_summary="speaker_A 讨论了一期范围。",
        topics=["一期范围"],
        candidate_scenes=["meeting", "todo"],
        evidence_segment_ids=[segment_id],
        boundary_confidence=0.9,
        local_date="2026-08-05",
        timezone="Asia/Shanghai",
    )


def test_user_speaker_reliability_starts_at_point_eight_five() -> None:
    below = UserSpeaker(
        speaker_id="speaker_A",
        confidence=0.84,
        reasoning="只有弱身份线索",
        evidence_segment_ids=["seg_001"],
    )
    boundary = UserSpeaker(
        speaker_id="speaker_A",
        confidence=0.85,
        reasoning="存在明确责任锚点",
        evidence_segment_ids=["seg_001"],
    )

    assert below.is_reliable is False
    assert boundary.is_reliable is True


@pytest.mark.parametrize(
    "evidence_segment_ids",
    [[], ["seg_001", "seg_001"]],
)
def test_reliable_user_speaker_requires_nonempty_unique_evidence(
    evidence_segment_ids: list[str],
) -> None:
    with pytest.raises(ValidationError, match="evidence"):
        UserSpeaker(
            speaker_id="speaker_A",
            confidence=0.85,
            reasoning="存在明确责任锚点",
            evidence_segment_ids=evidence_segment_ids,
        )


def test_event_rejects_unknown_fields_and_inverted_time_range() -> None:
    payload = event("event_001").model_dump(mode="json")
    payload["invented"] = "not allowed"

    with pytest.raises(ValidationError):
        Event.model_validate(payload)
    with pytest.raises(ValidationError):
        event("event_002", start_ms=1_000, end_ms=1_000)


def test_event_map_rejects_child_outside_parent_time_range() -> None:
    parent = event("event_001", start_ms=1_000, end_ms=3_000)
    child_payload = event("event_002", start_ms=500, end_ms=2_000).model_dump()
    child_payload["parent_event_id"] = "event_001"

    with pytest.raises(ValidationError):
        EventMap(
            user_speaker=UserSpeaker(
                speaker_id=None,
                confidence=0.3,
                reasoning="身份信号冲突",
                evidence_segment_ids=[],
            ),
            events=[parent, Event.model_validate(child_payload)],
            unassigned_segment_ids=[],
        )


def test_structured_segment_preserves_recording_context_and_unknown_date() -> None:
    segment = StructuredTranscriptSegment(
        segment_id="seg_00120",
        file_id="file_001",
        file_name="recording.mp3",
        recording_started_at=None,
        local_date=None,
        timezone=None,
        start_ms=34_200_000,
        end_ms=34_212_000,
        speaker_id="speaker_A",
        text="我们先确定第一阶段只支持上传已有音频。",
    )

    assert segment.local_date is None
    assert segment.recording_started_at is None
    with pytest.raises(ValidationError):
        StructuredTranscriptSegment.model_validate(
            {**segment.model_dump(mode="json"), "end_ms": segment.start_ms}
        )


def test_event_map_rejects_parent_cycles() -> None:
    first_payload = event("event_001").model_dump()
    second_payload = event("event_002").model_dump()
    first_payload["parent_event_id"] = "event_002"
    second_payload["parent_event_id"] = "event_001"

    with pytest.raises(ValidationError):
        EventMap(
            user_speaker=UserSpeaker(
                speaker_id="speaker_A",
                confidence=0.8,
                reasoning="存在明确责任锚点",
                evidence_segment_ids=["seg_001"],
            ),
            events=[Event.model_validate(first_payload), Event.model_validate(second_payload)],
            unassigned_segment_ids=[],
        )


def test_event_rejects_duplicate_evidence_ids() -> None:
    duplicate_payload = event("event_001").model_dump(mode="json")
    duplicate_payload["evidence_segment_ids"] = ["seg_001", "seg_001"]
    with pytest.raises(ValidationError):
        Event.model_validate(duplicate_payload)


def test_event_map_rejects_segment_that_is_both_assigned_and_unassigned() -> None:
    with pytest.raises(ValidationError):
        EventMap(
            user_speaker=UserSpeaker(
                speaker_id="speaker_A",
                confidence=0.8,
                reasoning="存在明确责任锚点",
                evidence_segment_ids=["seg_001"],
            ),
            events=[event("event_001")],
            unassigned_segment_ids=["seg_001"],
        )


def test_event_map_rejects_segment_assigned_to_two_independent_events() -> None:
    with pytest.raises(ValidationError, match="more than one event|multiple events"):
        EventMap(
            user_speaker=UserSpeaker(
                speaker_id="speaker_A",
                confidence=0.8,
                reasoning="存在明确责任锚点",
                evidence_segment_ids=["seg_001"],
            ),
            events=[
                event("event_001", segment_id="seg_001"),
                event("event_002", segment_id="seg_001"),
            ],
            unassigned_segment_ids=[],
        )


def test_event_map_rejects_parent_child_segment_overlap() -> None:
    with pytest.raises(ValidationError, match="more than one event|multiple events"):
        EventMap(
            user_speaker=UserSpeaker(
                speaker_id="speaker_A",
                confidence=0.8,
                reasoning="存在明确责任锚点",
                evidence_segment_ids=["seg_001"],
            ),
            events=[
                event(
                    "event_001",
                    segment_id="seg_001",
                    start_ms=0,
                    end_ms=2_000,
                ),
                event(
                    "event_002",
                    segment_id="seg_001",
                    parent_event_id="event_001",
                    start_ms=500,
                    end_ms=1_500,
                ),
            ],
            unassigned_segment_ids=[],
        )


def test_event_type_rejects_unregistered_value() -> None:
    with pytest.raises(ValidationError, match="event_type"):
        event("event_001", event_type="invented_user_commitment")
