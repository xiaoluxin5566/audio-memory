from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardShell(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1200)


class GroupedItem(StrictModel):
    title: str
    items: list[str]


class DetailSection(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    kind: Literal["text", "list", "grouped_items"]
    text: str | None = None
    items: list[str] | None = None
    groups: list[GroupedItem] | None = None


class TodoDraft(StrictModel):
    text: str
    assignee: str | None = None
    due_at: str | None = None


class EvidenceRef(StrictModel):
    file_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class SceneResult(StrictModel):
    scene_id: Literal["todo", "meeting", "parenting", "content", "growth", "inspiration"]
    should_generate: bool
    card: CardShell | None = None
    detail_sections: list[DetailSection] = Field(default_factory=list)
    todos: list[TodoDraft] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

