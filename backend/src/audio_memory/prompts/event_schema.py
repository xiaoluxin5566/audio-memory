from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SceneId = Literal[
    "todo", "meeting", "parenting", "content", "growth", "inspiration"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserSpeaker(StrictModel):
    speaker_id: str | None
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=1, max_length=500)
    evidence_segment_ids: list[str]

    @property
    def is_reliable(self) -> bool:
        return self.speaker_id is not None and self.confidence >= 0.70


class StructuredTranscriptSegment(StrictModel):
    segment_id: str = Field(pattern=r"^seg_[A-Za-z0-9_]+$")
    file_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    recording_started_at: datetime | None
    local_date: date | None
    timezone: str | None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    speaker_id: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> StructuredTranscriptSegment:
        if self.end_ms <= self.start_ms:
            raise ValueError("segment end_ms must be greater than start_ms")
        return self


class Event(StrictModel):
    event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    parent_event_id: str | None
    event_type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    speaker_ids: list[str]
    user_role: str | None
    user_role_confidence: float = Field(ge=0, le=1)
    factual_summary: str = Field(min_length=1, max_length=2_000)
    topics: list[str]
    candidate_scenes: list[SceneId]
    evidence_segment_ids: list[str] = Field(min_length=1)
    boundary_confidence: float = Field(ge=0, le=1)
    local_date: date | None
    timezone: str | None

    @model_validator(mode="after")
    def validate_time_range(self) -> Event:
        if self.end_ms <= self.start_ms:
            raise ValueError("event end_ms must be greater than start_ms")
        if len(self.evidence_segment_ids) != len(set(self.evidence_segment_ids)):
            raise ValueError("event evidence_segment_ids must be unique")
        return self


class EventMap(StrictModel):
    user_speaker: UserSpeaker
    events: list[Event]
    unassigned_segment_ids: list[str]

    @model_validator(mode="after")
    def validate_event_graph(self) -> EventMap:
        events_by_id = {event.event_id: event for event in self.events}
        if len(events_by_id) != len(self.events):
            raise ValueError("event_id values must be unique")
        if len(self.unassigned_segment_ids) != len(set(self.unassigned_segment_ids)):
            raise ValueError("unassigned_segment_ids must be unique")
        assigned_segment_ids = {
            segment_id
            for event in self.events
            for segment_id in event.evidence_segment_ids
        }
        overlap = assigned_segment_ids & set(self.unassigned_segment_ids)
        if overlap:
            raise ValueError("a segment cannot be both assigned and unassigned")
        for event in self.events:
            if event.parent_event_id is None:
                continue
            parent = events_by_id.get(event.parent_event_id)
            if parent is None:
                raise ValueError("parent_event_id must reference an event in this map")
            if event.start_ms < parent.start_ms or event.end_ms > parent.end_ms:
                raise ValueError("child event must remain inside its parent time range")
            ancestors = {event.event_id}
            current = parent
            while current.parent_event_id is not None:
                if current.event_id in ancestors:
                    raise ValueError("event parent relationships must be acyclic")
                ancestors.add(current.event_id)
                current = events_by_id.get(current.parent_event_id)
                if current is None:
                    raise ValueError("parent_event_id must reference an event in this map")
            if current.event_id in ancestors:
                raise ValueError("event parent relationships must be acyclic")
        return self
