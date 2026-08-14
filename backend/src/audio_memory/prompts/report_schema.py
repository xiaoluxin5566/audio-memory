from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


UNSAFE_REPORT_PATTERNS = (
    r"<\s*(script|iframe|object|embed|img|svg|style)\b",
    r"\bon\w+\s*=",
    r"\]\(\s*(javascript|data|vbscript):",
)
THIRD_PERSON_REPORT_PATTERNS = (
    r"(?:^|[，。！？；：\s])用户(?:认为|表示|今天|当天|需要|应该|可能|出现|参加|提到|说)",
    r"本报告(?:认为|发现|显示|建议|指出|将)",
)
UNSUPPORTED_NONVERBAL_PATTERNS = (
    r"检测到.{0,12}(?:咳嗽|喷嚏|干呕|呕吐|哭声|哭闹声)",
    r"从语气.{0,12}(?:判断|看出|说明|显示)",
    r"(?:录音里|音频中).{0,8}听到.{0,12}(?:咳嗽|喷嚏|哭声|哭闹声)",
)


def validate_report_text(text: str, *, enforce_voice: bool = True) -> None:
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in UNSAFE_REPORT_PATTERNS):
        raise ValueError("report content contains unsafe HTML or URL")
    if enforce_voice and any(
        re.search(pattern, text) for pattern in THIRD_PERSON_REPORT_PATTERNS
    ):
        raise ValueError("report must use a direct second-person voice")
    if any(re.search(pattern, text) for pattern in UNSUPPORTED_NONVERBAL_PATTERNS):
        raise ValueError("transcript-only report cannot make nonverbal detection claims")


def validate_unique_ids(values: list[str], *, field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must be unique")


class StrictReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportTodo(StrictReportModel):
    text: str = Field(min_length=1, max_length=300)
    action: str = Field(min_length=1, max_length=160)
    object: str | None = Field(default=None, max_length=200)
    owner_type: Literal["user", "shared"]
    assignee_text: str | None = Field(default=None, max_length=160)
    due_at: datetime | None = None
    due_text: str | None = Field(default=None, max_length=160)
    dependency: str | None = Field(default=None, max_length=500)
    next_step: str | None = Field(default=None, max_length=500)
    source_scene_id: str = Field(min_length=1, max_length=100)
    evidence_segment_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.5, le=1)


class SingleReportDraft(StrictReportModel):
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=4_000)
    report_markdown: str = Field(min_length=1, max_length=120_000)
    todos: list[ReportTodo] = Field(default_factory=list)
    evidence_segment_ids: list[str] = Field(default_factory=list)
    external_source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_report(self):
        markdown = self.report_markdown
        validate_report_text(markdown)
        validate_unique_ids(
            self.evidence_segment_ids, field_name="evidence_segment_ids"
        )
        validate_unique_ids(
            self.external_source_ids, field_name="external_source_ids"
        )
        empty = (
            self.title == "本次内容报告"
            and self.summary == "本次内容无有价值信息"
            and self.report_markdown.strip() == "本次内容无有价值信息"
            and not self.todos
            and not self.evidence_segment_ids
            and not self.external_source_ids
        )
        if not empty and not markdown.lstrip().startswith("# "):
            raise ValueError("non-empty report_markdown must start with a level-one heading")
        return self


class EvidenceRequest(StrictReportModel):
    request_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=1_000)
    scene_ids: list[str] = Field(default_factory=list)
    evidence_segment_ids: list[str] = Field(default_factory=list)
    context_type: Literal[
        "scene_transcript", "evidence_comparison", "media_session", "external_source"
    ]
    writing_purpose: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_specific_request(self):
        if not self.scene_ids and not self.evidence_segment_ids:
            raise ValueError("EvidenceRequest requires specific evidence or scene IDs")
        generic = {"请再读全文", "再读全文", "阅读全文", "补充更多信息"}
        if self.question.strip() in generic:
            raise ValueError("EvidenceRequest requires specific evidence question")
        return self


class WriterAction(StrictReportModel):
    action: Literal["request_evidence", "submit_report"]
    evidence_request: EvidenceRequest | None = None
    report: SingleReportDraft | None = None

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action == "request_evidence":
            if self.evidence_request is None or self.report is not None:
                raise ValueError("request_evidence requires only evidence_request")
        elif self.report is None or self.evidence_request is not None:
            raise ValueError("submit_report requires only report")
        return self
