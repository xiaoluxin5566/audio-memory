from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AuditMode = Literal["chunk_v1_audit", "full_v1_audit", "revision_final_audit"]
AuditSeverity = Literal["critical", "major", "minor"]
ScoreDimension = Literal[
    "factual_accuracy",
    "important_coverage",
    "analysis_depth",
    "actionability",
    "expression_structure",
]
ValueOpportunityKind = Literal[
    "knowledge_enrichment",
    "analysis_deepening",
    "advice_rework",
    "structure_rewrite",
    "title_rewrite",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceExcerpt(_StrictModel):
    segment_id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=8_000)


class AuditIssue(_StrictModel):
    issue_id: str = Field(pattern=r"^issue_[A-Za-z0-9_]+$")
    severity: AuditSeverity
    issue_type: str = Field(min_length=1, max_length=80)
    section_id: str | None = Field(default=None, max_length=160)
    related_section_ids: list[str] = Field(default_factory=list, max_length=30)
    problem: str = Field(min_length=1, max_length=4_000)
    importance: str = Field(min_length=1, max_length=4_000)
    required_change: str = Field(min_length=1, max_length=4_000)
    affected_claims: list[str] = Field(default_factory=list, max_length=30)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_excerpts: list[EvidenceExcerpt] = Field(default_factory=list, max_length=100)
    context_excerpts: list[EvidenceExcerpt] = Field(default_factory=list, max_length=100)
    allow_deletion_or_compression: bool = False

    @model_validator(mode="after")
    def material_issue_has_revision_evidence(self) -> "AuditIssue":
        if not self.section_id:
            raise ValueError("audit issue requires a target section ID")
        if len(self.related_section_ids) != len(set(self.related_section_ids)):
            raise ValueError("related section IDs must be unique")
        if self.severity in {"critical", "major"}:
            if not self.evidence_segment_ids or not self.evidence_excerpts:
                raise ValueError("material issue requires an evidence packet")
            excerpt_ids = {item.segment_id for item in self.evidence_excerpts}
            if not set(self.evidence_segment_ids).issubset(excerpt_ids):
                raise ValueError("material issue evidence excerpts must cover evidence IDs")
        return self


class AuditValueOpportunity(_StrictModel):
    opportunity_id: str = Field(pattern=r"^opportunity_[A-Za-z0-9_]+$")
    kind: ValueOpportunityKind
    section_id: str = Field(min_length=1, max_length=160)
    current_gap: str = Field(min_length=1, max_length=4_000)
    desired_value: str = Field(min_length=1, max_length=4_000)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_excerpts: list[EvidenceExcerpt] = Field(default_factory=list, max_length=100)
    preserve_constraints: list[str] = Field(default_factory=list, max_length=30)
    allow_section_rewrite: bool = False

    @model_validator(mode="after")
    def validate_evidence_packet(self) -> "AuditValueOpportunity":
        if self.allow_section_rewrite and not self.evidence_excerpts:
            raise ValueError("section rewrite opportunity requires evidence")
        excerpt_ids = {item.segment_id for item in self.evidence_excerpts}
        if not set(self.evidence_segment_ids).issubset(excerpt_ids):
            raise ValueError("opportunity evidence excerpts must cover evidence IDs")
        return self


class AuditScores(_StrictModel):
    factual_accuracy: int = Field(ge=0, le=30)
    important_coverage: int = Field(ge=0, le=25)
    analysis_depth: int = Field(ge=0, le=20)
    actionability: int = Field(ge=0, le=15)
    expression_structure: int = Field(ge=0, le=10)
    total: int = Field(ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def recompute_total_from_dimensions(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        dimension_names = (
            "factual_accuracy",
            "important_coverage",
            "analysis_depth",
            "actionability",
            "expression_structure",
        )
        dimensions = [value.get(name) for name in dimension_names]
        if not all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in dimensions
        ):
            return value
        normalized = dict(value)
        normalized["total"] = sum(dimensions)
        return normalized


class AuditDeduction(_StrictModel):
    dimension: ScoreDimension
    points: int = Field(ge=1, le=30)
    reason: str = Field(min_length=1, max_length=2_000)


class AuditCoverage(_StrictModel):
    full_transcript_reviewed: bool
    reviewed_segment_count: int | None = Field(default=None, ge=0)
    total_segment_count: int | None = Field(default=None, ge=0)
    unreviewed_ranges: list[str] = Field(default_factory=list, max_length=200)
    summary: str = Field(min_length=1, max_length=4_000)


class ReportAudit(_StrictModel):
    audit_mode: AuditMode
    rubric_version: Literal[1]
    passed: bool
    scores: AuditScores
    deductions: list[AuditDeduction] = Field(default_factory=list, max_length=100)
    coverage: AuditCoverage
    issues: list[AuditIssue] = Field(default_factory=list, max_length=100)
    value_opportunities: list[AuditValueOpportunity] = Field(
        default_factory=list, max_length=100
    )
    unresolved_issue_ids: list[str] = Field(default_factory=list, max_length=100)
    summary: str = Field(default="审核完成。", min_length=1, max_length=4_000)

    @model_validator(mode="before")
    @classmethod
    def normalize_unresolved_issue_references(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        issues = value.get("issues")
        unresolved = value.get("unresolved_issue_ids")
        if not isinstance(issues, list) or not isinstance(unresolved, list):
            return value
        issue_ids = {
            item.get("issue_id") for item in issues if isinstance(item, dict)
        }
        if any(item not in issue_ids for item in unresolved):
            normalized = dict(value)
            normalized["unresolved_issue_ids"] = [
                item.get("issue_id")
                for item in issues
                if isinstance(item, dict) and item.get("issue_id")
            ]
            return normalized
        return value

    @model_validator(mode="after")
    def validate_audit_contract(self) -> "ReportAudit":
        issue_ids = [item.issue_id for item in self.issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("audit issue IDs must be unique")
        opportunity_ids = [item.opportunity_id for item in self.value_opportunities]
        if len(opportunity_ids) != len(set(opportunity_ids)):
            raise ValueError("audit opportunity IDs must be unique")
        if not set(self.unresolved_issue_ids).issubset(issue_ids):
            raise ValueError("unresolved issue IDs must reference audit issues")

        if self.audit_mode == "chunk_v1_audit":
            complete = (
                not self.coverage.full_transcript_reviewed
                and self.coverage.reviewed_segment_count is not None
                and self.coverage.total_segment_count is not None
                and self.coverage.reviewed_segment_count
                == self.coverage.total_segment_count
                and not self.coverage.unreviewed_ranges
            )
            if not complete:
                raise ValueError("chunk audit requires complete chunk coverage")
        elif self.audit_mode == "full_v1_audit":
            complete = (
                self.coverage.full_transcript_reviewed
                and self.coverage.reviewed_segment_count is not None
                and self.coverage.total_segment_count is not None
                and self.coverage.reviewed_segment_count
                == self.coverage.total_segment_count
                and not self.coverage.unreviewed_ranges
            )
            if not complete:
                raise ValueError("full V1 audit requires complete transcript coverage")
        elif self.coverage.full_transcript_reviewed:
            raise ValueError("bounded final audit cannot claim full transcript review")

        unresolved = {
            item.issue_id: item
            for item in self.issues
            if item.issue_id in self.unresolved_issue_ids
        }
        if any(item.severity == "critical" for item in unresolved.values()):
            if self.scores.total > 59:
                raise ValueError("unresolved critical issue caps score at 59")
        if any(
            item.severity == "major" and item.issue_type == "factual_error"
            for item in unresolved.values()
        ) and self.scores.total > 69:
            raise ValueError("unresolved major factual issue caps score at 69")

        expected_pass = self.scores.total >= 75 and not any(
            item.severity in {"critical", "major"}
            for item in unresolved.values()
        )
        if self.passed != expected_pass:
            raise ValueError("passed must match score and unresolved material issues")
        return self

    @property
    def material_issues(self) -> tuple[AuditIssue, ...]:
        return tuple(
            item for item in self.issues if item.severity in {"critical", "major"}
        )
