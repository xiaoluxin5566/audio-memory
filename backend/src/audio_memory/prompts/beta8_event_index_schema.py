from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Beta8SegmentRange(_StrictModel):
    source_file: str = Field(min_length=1, max_length=1_000)
    start_segment_id: str = Field(min_length=1, max_length=500)
    end_segment_id: str = Field(min_length=1, max_length=500)


ActivityKind = Literal[
    "conversation",
    "content_playback",
    "solo_speech_or_activity",
    "ambient_activity",
    "other",
]
CommunicationPurpose = Literal[
    "work",
    "non_work",
    "mixed",
    "uncertain",
    "not_applicable",
]
ParticipationMode = Literal[
    "user_present_live_interaction",
    "recorded_or_broadcast_content",
    "solo_user_activity",
    "ambient_or_third_party",
    "unknown",
]
StartBoundary = Literal[
    "input_start",
    "contact_started",
    "activity_started",
    "participant_or_context_changed",
    "resumed_after_end",
]
EndBoundary = Literal[
    "input_end",
    "contact_ended",
    "activity_ended",
    "participant_or_context_changed",
    "interrupted",
]
ExpressionMode = Literal[
    "user_experience",
    "third_party_case",
    "quotation",
    "media_playback",
    "model_content",
    "mixed",
    "unknown",
]
ExcludedReason = Literal["noise", "duplicate", "unintelligible", "empty"]


class Beta8TimelineSessionBlock(_StrictModel):
    start_segment_id: str = Field(min_length=1, max_length=500)
    end_segment_id: str = Field(min_length=1, max_length=500)
    disposition: Literal["session"]
    session_id: str = Field(min_length=1, max_length=500)


class Beta8TimelineExcludedBlock(_StrictModel):
    start_segment_id: str = Field(min_length=1, max_length=500)
    end_segment_id: str = Field(min_length=1, max_length=500)
    disposition: Literal["excluded"]
    excluded_reason: ExcludedReason


Beta8TimelineBlock = Annotated[
    Beta8TimelineSessionBlock | Beta8TimelineExcludedBlock,
    Field(discriminator="disposition"),
]


class Beta8PrimarySessionDraft(_StrictModel):
    session_id: str = Field(min_length=1, max_length=500)
    activity_kind: ActivityKind
    communication_purpose: CommunicationPurpose
    participation_mode: ParticipationMode
    start_boundary: StartBoundary
    start_boundary_evidence_segment_ids: list[str] = Field(
        min_length=1, max_length=8
    )
    end_boundary: EndBoundary
    end_boundary_evidence_segment_ids: list[str] = Field(
        min_length=1, max_length=8
    )
    subject: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_activity_relationships(self) -> "Beta8PrimarySessionDraft":
        if self.activity_kind == "conversation":
            if self.communication_purpose == "not_applicable":
                raise ValueError(
                    "conversation activity requires a communication purpose"
                )
        elif self.communication_purpose != "not_applicable":
            raise ValueError(
                "non-conversation activity requires communication purpose not_applicable"
            )
        expected_participation = {
            "conversation": "user_present_live_interaction",
            "content_playback": "recorded_or_broadcast_content",
            "solo_speech_or_activity": "solo_user_activity",
            "ambient_activity": "ambient_or_third_party",
        }.get(self.activity_kind)
        if (
            expected_participation is not None
            and self.participation_mode != expected_participation
        ):
            raise ValueError(
                f"{self.activity_kind} requires participation mode "
                f"{expected_participation}"
            )
        return self

    @property
    def is_work_communication(self) -> bool:
        return (
            self.activity_kind == "conversation"
            and self.participation_mode == "user_present_live_interaction"
            and self.communication_purpose in {"work", "mixed"}
        )


class Beta8EmbeddedEventDraft(_StrictModel):
    event_id: str = Field(min_length=1, max_length=500)
    parent_session_id: str = Field(min_length=1, max_length=500)
    event_kind: Literal[
        "content_playback",
        "third_party_case",
        "user_commentary",
        "discussion_topic",
        "other",
    ]
    expression_mode: ExpressionMode
    subject: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=300)
    evidence_ranges: list[Beta8SegmentRange] = Field(min_length=1, max_length=32)


class Beta8FileTimeline(_StrictModel):
    source_file: str = Field(min_length=1, max_length=1_000)
    blocks: list[Beta8TimelineBlock] = Field(min_length=1, max_length=256)


class Beta8EventIndexDraft(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    primary_sessions: list[Beta8PrimarySessionDraft] = Field(
        default_factory=list, max_length=128
    )
    embedded_events: list[Beta8EmbeddedEventDraft] = Field(
        default_factory=list, max_length=256
    )
    file_timelines: list[Beta8FileTimeline] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completion_and_global_bounds(self) -> "Beta8EventIndexDraft":
        if self.input_complete and self.input_error is not None:
            raise ValueError("input_error must be null when input_complete is true")
        if not self.input_complete:
            if not (self.input_error or "").strip():
                raise ValueError(
                    "input_error is required when input_complete is false"
                )
            if self.primary_sessions or self.embedded_events or self.file_timelines:
                raise ValueError("incomplete input cannot contain index content")
        if sum(len(timeline.blocks) for timeline in self.file_timelines) > 512:
            raise ValueError(
                "event index cannot contain more than 512 timeline blocks"
            )
        routeable_ids = [
            *(session.session_id for session in self.primary_sessions),
            *(event.event_id for event in self.embedded_events),
        ]
        if len(routeable_ids) != len(set(routeable_ids)):
            raise ValueError("primary session and embedded event IDs must be unique")
        return self


class Beta8PrimarySession(Beta8PrimarySessionDraft):
    ranges: list[Beta8SegmentRange] = Field(min_length=1, max_length=32)

    @property
    def unit_id(self) -> str:
        return self.session_id


class Beta8CoverageRange(_StrictModel):
    range: Beta8SegmentRange
    disposition: Literal["session", "excluded"]
    session_id: str | None = Field(default=None, min_length=1, max_length=500)
    excluded_reason: ExcludedReason | None = None
    origin: Literal["model", "system"]

    @model_validator(mode="after")
    def validate_disposition_payload(self) -> "Beta8CoverageRange":
        if self.disposition == "session":
            if self.session_id is None or self.excluded_reason is not None:
                raise ValueError("session coverage requires only session_id")
        elif self.session_id is not None or self.excluded_reason is None:
            raise ValueError("excluded coverage requires only excluded_reason")
        return self


class Beta8NormalizedEventIndex(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    primary_sessions: list[Beta8PrimarySession] = Field(max_length=128)
    embedded_events: list[Beta8EmbeddedEventDraft] = Field(max_length=256)
    coverage_ranges: list[Beta8CoverageRange] = Field(max_length=512)
    system_excluded_ranges: list[Beta8CoverageRange] = Field(max_length=128)

    @property
    def routeable_unit_ids(self) -> tuple[str, ...]:
        return tuple(
            [session.session_id for session in self.primary_sessions]
            + [event.event_id for event in self.embedded_events]
        )

    @property
    def work_communication_session_ids(self) -> tuple[str, ...]:
        return tuple(
            session.session_id
            for session in self.primary_sessions
            if session.is_work_communication
        )

    @property
    def units(self) -> list[Beta8PrimarySession]:
        """Temporary compatibility view during the two-layer migration."""
        return self.primary_sessions


class Beta8EventUnit(_StrictModel):
    unit_id: str = Field(min_length=1, max_length=500)
    activity_kind: Literal[
        "conversation",
        "content_playback",
        "solo_speech_or_activity",
        "ambient_activity",
        "other",
    ]
    communication_purpose: Literal[
        "work",
        "non_work",
        "mixed",
        "uncertain",
        "not_applicable",
    ]
    participation_mode: Literal[
        "user_present_live_interaction",
        "recorded_or_broadcast_content",
        "solo_user_activity",
        "ambient_or_third_party",
        "unknown",
    ]
    start_boundary: Literal[
        "input_start",
        "contact_started",
        "activity_started",
        "participant_or_context_changed",
        "resumed_after_end",
    ]
    end_boundary: Literal[
        "input_end",
        "contact_ended",
        "activity_ended",
        "participant_or_context_changed",
        "interrupted",
    ]
    ranges: list[Beta8SegmentRange] = Field(min_length=1, max_length=32)
    subject: str = Field(min_length=1, max_length=120)
    expression_mode: Literal[
        "user_experience",
        "third_party_case",
        "quotation",
        "media_playback",
        "model_content",
        "mixed",
        "unknown",
    ]
    description: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_communication_purpose(self) -> "Beta8EventUnit":
        if self.activity_kind == "conversation":
            if self.communication_purpose == "not_applicable":
                raise ValueError(
                    "conversation activity requires a communication purpose"
                )
        elif self.communication_purpose != "not_applicable":
            raise ValueError(
                "non-conversation activity requires communication purpose not_applicable"
            )
        expected_participation = {
            "conversation": "user_present_live_interaction",
            "content_playback": "recorded_or_broadcast_content",
            "solo_speech_or_activity": "solo_user_activity",
            "ambient_activity": "ambient_or_third_party",
        }.get(self.activity_kind)
        if (
            expected_participation is not None
            and self.participation_mode != expected_participation
        ):
            raise ValueError(
                f"{self.activity_kind} requires participation mode "
                f"{expected_participation}"
            )
        return self

    @property
    def is_work_communication(self) -> bool:
        return (
            self.activity_kind == "conversation"
            and self.communication_purpose in {"work", "mixed"}
        )


class Beta8ExcludedRange(_StrictModel):
    range: Beta8SegmentRange
    reason: Literal["noise", "duplicate", "unintelligible", "empty"]


class Beta8EventIndex(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    activity_sessions: list[Beta8EventUnit] = Field(
        default_factory=list, max_length=128
    )
    excluded_ranges: list[Beta8ExcludedRange] = Field(
        default_factory=list, max_length=128
    )

    @property
    def units(self) -> list[Beta8EventUnit]:
        """Internal compatibility view while downstream names migrate to sessions."""
        return self.activity_sessions
