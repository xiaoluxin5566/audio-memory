from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from audio_memory.prompts.report_schema import (
    StrictReportModel,
    validate_report_text,
    validate_unique_ids,
)


class ReviewIssue(StrictReportModel):
    issue_id: str = Field(pattern=r"^issue_[0-9]{3,}$")
    severity: Literal["critical", "major", "minor"]
    category: Literal[
        "missing_event",
        "missing_todo",
        "thin_analysis",
        "unsupported_claim",
        "identity_risk",
        "missing_quote",
        "generic_advice",
        "repetition",
        "structure",
    ]
    section_id: str = Field(pattern=r"^section_[0-9]{3}$")
    description: str = Field(min_length=1, max_length=1_000)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_issue(self):
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        validate_report_text(self.description, enforce_voice=False)
        return self


class SectionRevision(StrictReportModel):
    section_id: str = Field(pattern=r"^section_[0-9]{3}$")
    title: str = Field(min_length=1, max_length=240)
    revised_markdown: str = Field(min_length=1, max_length=60_000)
    change_kind: Literal["factual", "analysis", "correction", "style"]
    issues_resolved: list[str] = Field(min_length=1, max_length=100)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=200)
    preserved_facts: list[str] = Field(default_factory=list, max_length=100)
    preserved_quotes: list[str] = Field(default_factory=list, max_length=100)
    preserved_todos: list[str] = Field(default_factory=list, max_length=100)
    removes_repetition: bool = False
    repetition_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_revision(self):
        if self.revised_markdown.splitlines()[0].strip() != f"## {self.title.strip()}":
            raise ValueError("revised section must start with its exact level-two title")
        if self.change_kind in {"factual", "correction"} and not self.evidence_segment_ids:
            raise ValueError("factual revisions require evidence")
        if self.removes_repetition and not (
            self.repetition_reason and self.repetition_reason.strip()
        ):
            raise ValueError("repetition removal requires a reason")
        validate_unique_ids(self.issues_resolved, field_name="issues_resolved")
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        validate_report_text(self.title)
        validate_report_text(self.revised_markdown)
        return self


class DirectReportReview(StrictReportModel):
    review_passed: bool
    revised_title: str | None = Field(default=None, min_length=1, max_length=80)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=200)
    revised_sections: list[SectionRevision] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_review(self):
        if self.revised_title is not None:
            validate_report_text(self.revised_title)
            if self.revised_title.lstrip().startswith("#"):
                raise ValueError("revised title must not contain Markdown heading markers")
        issue_ids = [item.issue_id for item in self.issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("review issue IDs must be unique")
        section_ids = [item.section_id for item in self.revised_sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("revised section IDs must be unique")
        known_issues = set(issue_ids)
        for revision in self.revised_sections:
            unknown = set(revision.issues_resolved) - known_issues
            if unknown:
                raise ValueError(f"revision resolves unknown issues: {sorted(unknown)}")
        if self.review_passed and (
            self.revised_title
            or
            self.revised_sections
            or any(item.severity in {"critical", "major"} for item in self.issues)
        ):
            raise ValueError("passed review cannot contain material issues or revisions")
        return self
