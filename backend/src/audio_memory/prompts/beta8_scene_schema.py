from __future__ import annotations

from collections import Counter
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BETA8_SCENE_IDS = (
    "work_communication",
    "parenting_family",
    "health_state",
    "content_consumption",
    "inspiration_insight",
    "self_growth",
    "life_decisions",
)
Beta8SceneId = Literal[
    "work_communication",
    "parenting_family",
    "health_state",
    "content_consumption",
    "inspiration_insight",
    "self_growth",
    "life_decisions",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Beta8SearchCandidate(_StrictModel):
    question: str = Field(min_length=1, max_length=2_000)
    purpose: str = Field(min_length=1, max_length=2_000)
    related_segment_ids: list[str] = Field(default_factory=list, max_length=2_000)


class Beta8TodoCandidate(_StrictModel):
    text: str = Field(min_length=1, max_length=2_000)
    owner_type: Literal["user", "other_requires_user_follow_up"]
    assignee_text: str | None = Field(default=None, max_length=500)
    due_at: str | None = Field(default=None, max_length=100)
    due_text: str | None = Field(default=None, max_length=500)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=2_000)


class Beta8SceneCard(_StrictModel):
    markdown: str = Field(min_length=1, max_length=80_000)
    source_segment_ids: list[str] = Field(default_factory=list, max_length=10_000)
    search_candidates: list[Beta8SearchCandidate] = Field(default_factory=list, max_length=5)


class Beta8SceneResult(_StrictModel):
    cards: list[Beta8SceneCard] = Field(default_factory=list, max_length=50)
    todo_candidates: list[Beta8TodoCandidate] = Field(default_factory=list, max_length=200)
    skip_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_skip_reason(self) -> "Beta8SceneResult":
        if self.cards and self.skip_reason is not None:
            raise ValueError("skip_reason must be null when cards exist")
        if not self.cards and not (self.skip_reason or "").strip():
            raise ValueError("skip_reason is required when cards is empty")
        return self


class Beta8CardBasis(_StrictModel):
    type: Literal["work_communication", "independent_value"]
    source_unit_ids: list[str] = Field(min_length=1, max_length=128)
    communication_kind: Literal[
        "meeting",
        "visit",
        "call",
        "online",
        "one_on_one",
        "interview",
        "customer",
        "supplier",
        "other",
    ] | None = None

    @model_validator(mode="after")
    def validate_basis(self) -> "Beta8CardBasis":
        if self.type == "work_communication" and self.communication_kind is None:
            raise ValueError("work communication basis requires communication_kind")
        if self.type == "independent_value" and self.communication_kind is not None:
            raise ValueError("independent value basis requires null communication_kind")
        return self


class Beta8UnifiedCard(Beta8SceneCard):
    draft_card_key: str = Field(min_length=1, max_length=500)
    card_basis: Beta8CardBasis


class Beta8UnifiedSceneResult(_StrictModel):
    scene_id: Beta8SceneId
    cards: list[Beta8UnifiedCard] = Field(default_factory=list)
    todo_candidates: list[Beta8TodoCandidate] = Field(default_factory=list, max_length=200)
    skip_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_skip_reason(self) -> "Beta8UnifiedSceneResult":
        if self.cards and self.skip_reason is not None:
            raise ValueError("skip_reason must be null when cards exist")
        if not self.cards and not (self.skip_reason or "").strip():
            raise ValueError("skip_reason is required when cards is empty")
        return self


class Beta8OmittedUnit(_StrictModel):
    unit_id: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=300)


class Beta8UnifiedV1Result(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    scene_results: list[Beta8UnifiedSceneResult] = Field(
        min_length=7,
        max_length=7,
    )
    omitted_units: list[Beta8OmittedUnit] = Field(
        default_factory=list,
        max_length=384,
    )

    @model_validator(mode="after")
    def validate_complete_ordered_scene_set(self) -> "Beta8UnifiedV1Result":
        actual = tuple(scene.scene_id for scene in self.scene_results)
        if actual != BETA8_SCENE_IDS:
            raise ValueError(
                "scene_results must contain exactly seven ordered scenes"
            )
        draft_card_keys = [
            card.draft_card_key
            for scene in self.scene_results
            for card in scene.cards
        ]
        if len(draft_card_keys) != len(set(draft_card_keys)):
            raise ValueError("draft_card_key values must be unique")
        if self.input_complete and self.input_error is not None:
            raise ValueError("complete input cannot have input_error")
        if not self.input_complete and not (self.input_error or "").strip():
            raise ValueError("incomplete input requires input_error")
        return self


class ParsedCardMarkdown(_StrictModel):
    title: str
    summary: str


_MARKDOWN_MARKER_RE = re.compile(r"(?:\*\*|__|`|\*|_)")


def parse_beta8_card_markdown(markdown: str) -> ParsedCardMarkdown:
    lines = markdown.splitlines()
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None or not re.fullmatch(r"#\s+\S.*", lines[first_index].strip()):
        raise ValueError("first non-empty line must be a level-one Markdown heading")
    title = lines[first_index].strip()[1:].strip()
    summary_lines: list[str] = []
    for line in lines[first_index + 1 :]:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+", stripped):
            break
        summary_lines.append(stripped)
    while summary_lines and not summary_lines[0]:
        summary_lines.pop(0)
    while summary_lines and not summary_lines[-1]:
        summary_lines.pop()
    if summary_lines and summary_lines[0].rstrip("：:").strip() == "核心摘要":
        summary_lines.pop(0)
        while summary_lines and not summary_lines[0]:
            summary_lines.pop(0)
    summary = " ".join(line for line in summary_lines if line)
    summary = _MARKDOWN_MARKER_RE.sub("", summary).strip()
    if not summary:
        raise ValueError("card Markdown requires a non-empty core summary")
    return ParsedCardMarkdown(title=title, summary=summary)


def validate_scene_result_ids(
    result: Beta8SceneResult, *, known_segment_ids: set[str]
) -> None:
    referenced = {
        segment_id
        for card in result.cards
        for segment_id in (
            card.source_segment_ids
            + [item for candidate in card.search_candidates for item in candidate.related_segment_ids]
        )
    }
    referenced.update(
        segment_id
        for candidate in result.todo_candidates
        for segment_id in candidate.evidence_segment_ids
    )
    unknown = sorted(referenced - known_segment_ids)
    if unknown:
        raise ValueError(f"Unknown transcript segment IDs: {', '.join(unknown)}")


def validate_unified_v1_against_index(
    result: Beta8UnifiedV1Result, event_index: "Beta8NormalizedEventIndex"
) -> None:
    from audio_memory.prompts.beta8_event_index_schema import Beta8NormalizedEventIndex

    if not isinstance(event_index, Beta8NormalizedEventIndex):
        raise TypeError("event_index must be a Beta8NormalizedEventIndex")

    routeable_ids = set(event_index.routeable_unit_ids)
    work_session_ids = set(event_index.work_communication_session_ids)
    referenced_unit_ids: set[str] = set()
    work_unit_card_counts: dict[str, int] = {}

    for scene in result.scene_results:
        for card in scene.cards:
            basis = card.card_basis
            referenced_unit_ids.update(basis.source_unit_ids)
            if scene.scene_id == "work_communication" and basis.type == "independent_value":
                work_communication_unit_ids = sorted(
                    unit_id
                    for unit_id in basis.source_unit_ids
                    if unit_id in work_session_ids
                )
                if work_communication_unit_ids:
                    raise ValueError(
                        "work scene independent value card cannot reference work "
                        "communication units: "
                        + ", ".join(work_communication_unit_ids)
                    )
            if basis.type != "work_communication":
                continue
            if scene.scene_id != "work_communication":
                raise ValueError(
                    "work communication card must be in work_communication scene"
                )
            referenced_work_sessions = sorted(
                set(basis.source_unit_ids) & work_session_ids
            )
            if len(referenced_work_sessions) != 1:
                raise ValueError(
                    "work communication card must reference exactly one work communication unit"
                )
            unit_id = referenced_work_sessions[0]
            work_unit_card_counts[unit_id] = work_unit_card_counts.get(unit_id, 0) + 1

    omitted_ids = [item.unit_id for item in result.omitted_units]
    omission_counts = Counter(omitted_ids)
    duplicate_omissions = sorted({
        unit_id for unit_id, count in omission_counts.items() if count > 1
    })
    if duplicate_omissions:
        raise ValueError(
            "index units must be omitted exactly once: "
            + ", ".join(duplicate_omissions)
        )

    omitted_work_units = sorted(
        unit_id
        for unit_id in omitted_ids
        if unit_id in work_session_ids
    )
    if omitted_work_units:
        raise ValueError(
            "work communication units cannot be omitted: "
            + ", ".join(omitted_work_units)
        )

    all_declared_ids = referenced_unit_ids | set(omitted_ids)
    unknown = sorted(all_declared_ids - routeable_ids)
    if unknown:
        raise ValueError(f"unknown index unit IDs: {', '.join(unknown)}")

    both = sorted(referenced_unit_ids & set(omitted_ids))
    if both:
        raise ValueError(
            "index units cannot be both referenced and omitted: " + ", ".join(both)
        )

    duplicate_work_cards = sorted(
        unit_id for unit_id, count in work_unit_card_counts.items() if count != 1
    )
    if duplicate_work_cards:
        raise ValueError(
            "each work communication unit must map to exactly one card: "
            + ", ".join(duplicate_work_cards)
        )

    missing_work_cards = sorted(
        unit_id
        for unit_id in work_session_ids
        if work_unit_card_counts.get(unit_id, 0) == 0
    )
    if missing_work_cards:
        raise ValueError(
            "each work communication unit requires exactly one work card: "
            + ", ".join(missing_work_cards)
        )

    unaccounted = sorted(routeable_ids - all_declared_ids)
    if unaccounted:
        raise ValueError(f"unaccounted index units: {', '.join(unaccounted)}")


def validate_unified_v1(
    result: Beta8UnifiedV1Result,
    *,
    known_segment_ids: set[str],
    event_index: "Beta8NormalizedEventIndex",
) -> None:
    validate_unified_v1_against_index(result, event_index)
    referenced: set[str] = set()
    for scene in result.scene_results:
        for card in scene.cards:
            parse_beta8_card_markdown(card.markdown)
            referenced.update(card.source_segment_ids)
            referenced.update(
                segment_id
                for candidate in card.search_candidates
                for segment_id in candidate.related_segment_ids
            )
        referenced.update(
            segment_id
            for candidate in scene.todo_candidates
            for segment_id in candidate.evidence_segment_ids
        )
    unknown = sorted(referenced - known_segment_ids)
    if unknown:
        raise ValueError(f"Unknown transcript segment IDs: {', '.join(unknown)}")


def normalize_uniquely_misindexed_segment_ids(
    result: Beta8SceneResult, *, known_segment_ids: set[str]
) -> Beta8SceneResult:
    by_segment_index: dict[str, list[str]] = {}
    for segment_id in known_segment_ids:
        match = re.fullmatch(r"seg_\d+_(\d+)", segment_id)
        if match is not None:
            by_segment_index.setdefault(match.group(1), []).append(segment_id)

    def normalize(segment_id: str) -> str:
        if segment_id in known_segment_ids:
            return segment_id
        match = re.fullmatch(r"seg_\d+_(\d+)", segment_id)
        if match is None:
            return segment_id
        candidates = by_segment_index.get(match.group(1), [])
        return candidates[0] if len(candidates) == 1 else segment_id

    payload = result.model_dump(mode="json")
    for card in payload["cards"]:
        card["source_segment_ids"] = [
            normalize(segment_id) for segment_id in card["source_segment_ids"]
        ]
        for candidate in card["search_candidates"]:
            candidate["related_segment_ids"] = [
                normalize(segment_id)
                for segment_id in candidate["related_segment_ids"]
            ]
    for candidate in payload["todo_candidates"]:
        candidate["evidence_segment_ids"] = [
            normalize(segment_id)
            for segment_id in candidate["evidence_segment_ids"]
        ]
    return Beta8SceneResult.model_validate(payload)
