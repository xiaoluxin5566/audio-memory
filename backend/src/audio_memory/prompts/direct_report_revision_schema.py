from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetedSectionRevision(_StrictModel):
    section_id: str = Field(pattern=r"^section_\d{3}$")
    title: str = Field(min_length=1, max_length=160)
    revised_markdown: str = Field(min_length=1, max_length=100_000)
    issues_resolved: list[str] = Field(default_factory=list, max_length=100)
    opportunities_resolved: list[str] = Field(default_factory=list, max_length=100)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=200)
    removes_repetition: bool = False
    repetition_reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_heading_and_repetition(self) -> "TargetedSectionRevision":
        expected = f"## {self.title}"
        if self.revised_markdown.splitlines()[0].strip() != expected:
            raise ValueError("revised Markdown heading must match title")
        if self.removes_repetition and not (
            self.repetition_reason and self.repetition_reason.strip()
        ):
            raise ValueError("repetition removal requires a reason")
        if not self.removes_repetition and self.repetition_reason is not None:
            raise ValueError("repetition reason requires removes_repetition")
        return self


class TargetedReportRevision(_StrictModel):
    revisions: list[TargetedSectionRevision] = Field(default_factory=list, max_length=30)
    unresolved_issue_ids: list[str] = Field(default_factory=list, max_length=100)
    revision_summary: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def validate_unique_coverage(self) -> "TargetedReportRevision":
        section_ids = [item.section_id for item in self.revisions]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("duplicate section revision")
        resolved = {
            issue_id for item in self.revisions for issue_id in item.issues_resolved
        }
        overlap = resolved & set(self.unresolved_issue_ids)
        if overlap:
            raise ValueError("issue cannot be both resolved and unresolved")
        if len(self.unresolved_issue_ids) != len(set(self.unresolved_issue_ids)):
            raise ValueError("unresolved issue IDs must be unique")
        opportunities = [
            opportunity_id
            for item in self.revisions
            for opportunity_id in item.opportunities_resolved
        ]
        if len(opportunities) != len(set(opportunities)):
            raise ValueError("opportunity cannot be resolved in multiple sections")
        return self
