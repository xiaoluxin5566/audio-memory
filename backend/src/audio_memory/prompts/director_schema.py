from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from audio_memory.prompts.event_schema import SceneId, StrictModel


ValueSignal = Literal[
    "explicit_decision",
    "explicit_commitment",
    "follow_up_needed",
    "open_question",
    "disagreement",
    "dependency_or_blocker",
    "role_or_org_change",
    "interview_or_career",
    "child_learning_difficulty",
    "emotional_signal",
    "relationship_pattern",
    "identifiable_content",
    "user_reaction",
    "new_idea",
    "behavior_with_outcome",
    "cross_scene_connection",
]
Priority = Literal["high", "medium", "low"]


class DirectorSelection(StrictModel):
    selection_id: str = Field(pattern=r"^selection_[A-Za-z0-9_]+$")
    cluster_ids: list[str] = Field(min_length=1)
    source_event_ids: list[str]
    candidate_scenes: list[SceneId] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    selection_reason: str = Field(min_length=1, max_length=1_000)
    value_signals: list[ValueSignal] = Field(min_length=1)
    priority: Priority
    context_before_clusters: int = Field(ge=0, le=1)
    context_after_clusters: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_identifiers_and_values(self) -> DirectorSelection:
        if any(
            not cluster_id.startswith("cluster_")
            or len(cluster_id) <= len("cluster_")
            for cluster_id in self.cluster_ids
        ):
            raise ValueError("cluster_ids must use stable cluster identifiers")
        if any(
            not event_id.startswith("event_")
            or len(event_id) <= len("event_")
            for event_id in self.source_event_ids
        ):
            raise ValueError("source_event_ids must use event identifiers")
        for label, values in (
            ("cluster_ids", self.cluster_ids),
            ("source_event_ids", self.source_event_ids),
            ("candidate_scenes", self.candidate_scenes),
            ("value_signals", self.value_signals),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        return self


class DirectorResult(StrictModel):
    selections: list[DirectorSelection]

    @model_validator(mode="after")
    def validate_selection_ids(self) -> DirectorResult:
        identifiers = [item.selection_id for item in self.selections]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("selection_id values must be unique")
        return self
