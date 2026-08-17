from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Literal

from audio_memory.analysis.direct_report_sections import (
    apply_section_revisions,
    split_report_sections,
)
from audio_memory.prompts.direct_report_audit_schema import ReportAudit
from audio_memory.prompts.direct_report_revision_schema import TargetedReportRevision


ReportVersion = Literal["v1", "v2"]
AuditStatus = Literal[
    "completed",
    "completed_unaudited",
    "completed_v1_revision_failed",
    "completed_v2_final_audit_degraded",
]
ScoreScope = Literal["v1_full_audit", "v1_pre_revision", "v2_final_audit"]


def sanitize_audit_evidence(
    audit: ReportAudit,
    transcript_by_id: dict[str, str],
) -> ReportAudit:
    """Remove model-written transcript citations that do not match their IDs."""
    payload = audit.model_dump(mode="json")
    retained_issues: list[dict[str, object]] = []
    for issue in payload["issues"]:
        valid_transcript_ids: set[str] = set()
        retained_excerpts: list[dict[str, str]] = []
        for excerpt in issue["evidence_excerpts"]:
            segment_id = excerpt["segment_id"]
            if segment_id.startswith("report_section_"):
                retained_excerpts.append(excerpt)
                continue
            source = transcript_by_id.get(segment_id)
            if source is None:
                continue
            candidate = excerpt["text"]
            if candidate in source or SequenceMatcher(None, candidate, source).ratio() >= 0.7:
                retained_excerpts.append(excerpt)
                valid_transcript_ids.add(segment_id)
        issue["evidence_excerpts"] = retained_excerpts
        issue["evidence_segment_ids"] = [
            segment_id
            for segment_id in issue["evidence_segment_ids"]
            if segment_id.startswith("report_section_")
            or segment_id in valid_transcript_ids
        ]
        if retained_excerpts:
            retained_issues.append(issue)
    payload["issues"] = retained_issues
    for opportunity in payload["value_opportunities"]:
        valid_transcript_ids: set[str] = set()
        retained_excerpts: list[dict[str, str]] = []
        for excerpt in opportunity["evidence_excerpts"]:
            segment_id = excerpt["segment_id"]
            if segment_id.startswith("report_section_"):
                retained_excerpts.append(excerpt)
                continue
            source = transcript_by_id.get(segment_id)
            if source is None:
                continue
            candidate = excerpt["text"]
            if candidate in source or SequenceMatcher(None, candidate, source).ratio() >= 0.7:
                retained_excerpts.append(excerpt)
                valid_transcript_ids.add(segment_id)
        opportunity["evidence_excerpts"] = retained_excerpts
        opportunity["evidence_segment_ids"] = [
            segment_id
            for segment_id in opportunity["evidence_segment_ids"]
            if segment_id.startswith("report_section_")
            or segment_id in valid_transcript_ids
        ]
        if opportunity["allow_section_rewrite"] and not retained_excerpts:
            opportunity["allow_section_rewrite"] = False
    retained_ids = {item["issue_id"] for item in retained_issues}
    payload["unresolved_issue_ids"] = [
        item for item in payload["unresolved_issue_ids"] if item in retained_ids
    ]
    unresolved_material = any(
        item["issue_id"] in payload["unresolved_issue_ids"]
        and item["severity"] in {"critical", "major"}
        for item in retained_issues
    )
    payload["passed"] = payload["scores"]["total"] >= 75 and not unresolved_material
    return ReportAudit.model_validate(payload)


def canonicalize_audit_evidence(
    audit: ReportAudit,
    transcript_by_id: dict[str, str],
    *,
    report_markdown: str | None = None,
) -> ReportAudit:
    payload = audit.model_dump(mode="json")
    report_sections = {
        f"report_{item.section_id}": item.markdown
        for item in split_report_sections(report_markdown or "")
    }
    evidence_collections = [
        (issue, ("evidence_excerpts", "context_excerpts"))
        for issue in payload["issues"]
    ] + [
        (opportunity, ("evidence_excerpts",))
        for opportunity in payload["value_opportunities"]
    ]
    for item, collection_names in evidence_collections:
        for collection_name in collection_names:
            for excerpt in item[collection_name]:
                segment_id = excerpt["segment_id"]
                if segment_id.startswith("report_section_"):
                    source = report_sections.get(segment_id)
                    if source is None:
                        raise ValueError(
                            f"unknown report audit evidence ID: {segment_id}"
                        )
                    if excerpt["text"] not in source:
                        excerpt["text"] = source[:8_000]
                    continue
                source = transcript_by_id.get(segment_id)
                if source is None:
                    raise ValueError(f"unknown audit evidence ID: {segment_id}")
                candidate = excerpt["text"]
                if candidate in source:
                    continue
                similarity = SequenceMatcher(None, candidate, source).ratio()
                if similarity < 0.7:
                    raise ValueError(
                        f"audit evidence is not near-verbatim: {segment_id}"
                    )
                excerpt["text"] = source
    return ReportAudit.model_validate(payload)


def validate_audit_evidence(
    audit: ReportAudit,
    *,
    transcript_by_id: dict[str, str],
    report_markdown: str,
) -> None:
    report_sections = {
        f"report_{item.section_id}": item.markdown
        for item in split_report_sections(report_markdown)
    }
    for issue in audit.issues:
        for excerpt in (*issue.evidence_excerpts, *issue.context_excerpts):
            if excerpt.segment_id.startswith("report_section_"):
                source = report_sections.get(excerpt.segment_id)
                if source is None or excerpt.text not in source:
                    raise ValueError(
                        "audit evidence does not match report section: "
                        f"{excerpt.segment_id}"
                    )
                continue
            source = transcript_by_id.get(excerpt.segment_id)
            if source is None or excerpt.text not in source:
                raise ValueError(
                    f"audit evidence does not match transcript: {excerpt.segment_id}"
                )
    for opportunity in audit.value_opportunities:
        for excerpt in opportunity.evidence_excerpts:
            if excerpt.segment_id.startswith("report_section_"):
                source = report_sections.get(excerpt.segment_id)
                if source is None or excerpt.text not in source:
                    raise ValueError(
                        "audit evidence does not match report section: "
                        f"{excerpt.segment_id}"
                    )
                continue
            source = transcript_by_id.get(excerpt.segment_id)
            if source is None or excerpt.text not in source:
                raise ValueError(
                    f"audit evidence does not match transcript: {excerpt.segment_id}"
                )


def audit_transcript_evidence_ids(
    audit: ReportAudit, valid_segment_ids: set[str]
) -> set[str]:
    referenced = {
        segment_id
        for issue in audit.issues
        for segment_id in issue.evidence_segment_ids
    } | {
        excerpt.segment_id
        for issue in audit.issues
        for excerpt in issue.context_excerpts
    } | {
        segment_id
        for opportunity in audit.value_opportunities
        for segment_id in opportunity.evidence_segment_ids
    }
    return referenced & valid_segment_ids


def revision_target_section_ids(
    report_markdown: str, audit: ReportAudit
) -> set[str]:
    available = {item.section_id for item in split_report_sections(report_markdown)}
    targets = {
        section_id
        for issue in audit.issues
        for section_id in (issue.section_id, *issue.related_section_ids)
        if section_id
    } | {
        opportunity.section_id for opportunity in audit.value_opportunities
    }
    unknown = targets - available
    if unknown:
        raise ValueError(f"audit references unknown report sections: {sorted(unknown)}")
    return targets


@dataclass(frozen=True, slots=True)
class ReportQualityMetadata:
    report_version: ReportVersion
    audit_status: AuditStatus
    quality_score: int | None
    quality_score_scope: ScoreScope | None
    quality_passed: bool | None
    component_scores: dict[str, int] | None = None
    issue_counts: dict[str, int] | None = None
    unresolved_issue_ids: tuple[str, ...] = ()
    degraded_reason: str | None = None

    def __post_init__(self) -> None:
        if self.quality_score is None:
            if self.quality_score_scope is not None or self.quality_passed is not None:
                raise ValueError("score scope and pass state require a quality score")
        else:
            if not 0 <= self.quality_score <= 100:
                raise ValueError("quality score must be between 0 and 100")
            if self.quality_score_scope is None:
                raise ValueError("quality score requires a score scope")
        if self.audit_status == "completed_unaudited" and self.quality_score is not None:
            raise ValueError("unaudited report cannot carry a quality score")
        if (
            self.audit_status == "completed_v2_final_audit_degraded"
            and self.quality_score_scope != "v1_pre_revision"
        ):
            raise ValueError("degraded V2 final audit must scope score to V1")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def audit_title_revision_section_ids(
    report_markdown: str, audit: ReportAudit
) -> set[str]:
    original_sections = {
        item.section_id: item for item in split_report_sections(report_markdown)
    }
    return {
        issue.section_id
        for issue in audit.issues
        if issue.section_id in original_sections
        and any(
            marker in f"{issue.issue_type} {issue.problem} {issue.required_change}".lower()
            for marker in ("title", "标题")
        )
        and any(
            excerpt.segment_id == f"report_{issue.section_id}"
            and excerpt.text.strip().startswith("## ")
            for excerpt in issue.evidence_excerpts
        )
    } | {
        opportunity.section_id
        for opportunity in audit.value_opportunities
        if opportunity.kind == "title_rewrite"
    }


def apply_audited_revision(
    v1_markdown: str,
    audit: ReportAudit,
    revision: TargetedReportRevision,
    valid_segment_ids: set[str],
) -> str:
    if audit.audit_mode != "full_v1_audit":
        raise ValueError("targeted revision requires a full V1 audit")
    audit_issue_ids = {item.issue_id for item in audit.issues}
    resolved_ids = {
        issue_id
        for item in revision.revisions
        for issue_id in item.issues_resolved
    }
    accounted = resolved_ids | set(revision.unresolved_issue_ids)
    missing = audit_issue_ids - accounted
    if missing:
        raise ValueError(f"audit issues are unaccounted: {sorted(missing)}")
    unknown_issue_ids = accounted - {item.issue_id for item in audit.issues}
    if unknown_issue_ids:
        raise ValueError(f"revision references unknown issues: {sorted(unknown_issue_ids)}")

    opportunities_by_id = {
        item.opportunity_id: item for item in audit.value_opportunities
    }
    allowed_title_change_ids = audit_title_revision_section_ids(v1_markdown, audit)
    resolved_opportunity_ids = {
        opportunity_id
        for item in revision.revisions
        for opportunity_id in item.opportunities_resolved
    }
    unknown_opportunity_ids = resolved_opportunity_ids - set(opportunities_by_id)
    if unknown_opportunity_ids:
        raise ValueError(
            "revision references unknown opportunities: "
            f"{sorted(unknown_opportunity_ids)}"
        )

    allowed_audit_evidence = {
        segment_id
        for item in audit.issues
        for segment_id in item.evidence_segment_ids
    } | {
        excerpt.segment_id
        for item in audit.issues
        for excerpt in item.context_excerpts
    } | {
        segment_id
        for item in audit.value_opportunities
        for segment_id in item.evidence_segment_ids
    }
    issues_by_id = {item.issue_id: item for item in audit.issues}
    for section in revision.revisions:
        wrong_section_opportunities = {
            opportunity_id
            for opportunity_id in section.opportunities_resolved
            if opportunities_by_id[opportunity_id].section_id != section.section_id
        }
        if wrong_section_opportunities:
            raise ValueError(
                "value opportunity section does not match revision section: "
                f"{sorted(wrong_section_opportunities)}"
            )
        outside = set(section.evidence_segment_ids) - allowed_audit_evidence
        if outside:
            raise ValueError(
                f"revision evidence is outside audit evidence: {sorted(outside)}"
            )
        must_replace_issue_types = (
            "factual", "misattribution", "process", "事实", "归因", "泄露"
        )
        remaining_claims = {
            claim
            for issue_id in section.issues_resolved
            for claim in issues_by_id[issue_id].affected_claims
            if issues_by_id[issue_id].allow_deletion_or_compression
            or any(
                marker in issues_by_id[issue_id].issue_type.lower()
                for marker in must_replace_issue_types
            )
            if claim in section.revised_markdown
        }
        if remaining_claims:
            raise ValueError(
                "affected claim remains in revised section: "
                f"{sorted(remaining_claims)}"
            )
        original = next(
            (item for item in split_report_sections(v1_markdown)
             if item.section_id == section.section_id),
            None,
        )
        deletion_allowed = section.removes_repetition and all(
            next(
                (issue.allow_deletion_or_compression for issue in audit.issues
                 if issue.issue_id == issue_id),
                False,
            )
            for issue_id in section.issues_resolved
        )
        if (
            original is not None
            and len(section.revised_markdown) < len(original.markdown) * 0.5
            and not deletion_allowed
        ):
            raise ValueError(f"revision abnormally compressed section: {section.section_id}")
    return apply_section_revisions(
        v1_markdown,
        tuple(revision.revisions),
        valid_segment_ids,
        allowed_title_change_ids=allowed_title_change_ids,
    )


def build_section_diffs(
    before_markdown: str,
    after_markdown: str,
    *,
    allowed_title_change_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    before = {item.section_id: item for item in split_report_sections(before_markdown)}
    after = {item.section_id: item for item in split_report_sections(after_markdown)}
    if set(before) != set(after):
        raise ValueError("revision changed report section topology")
    diffs: list[dict[str, object]] = []
    for section_id, original in before.items():
        revised = after[section_id]
        if (
            original.title != revised.title
            and section_id not in (allowed_title_change_ids or set())
        ):
            raise ValueError(f"revision changed section title: {section_id}")
        if original.markdown != revised.markdown:
            diffs.append(
                {
                    "section_id": section_id,
                    "title": original.title,
                    "before": original.markdown,
                    "after": revised.markdown,
                }
            )
    return diffs


def metadata_from_audit(
    *,
    report_version: ReportVersion,
    audit: ReportAudit,
    score_scope: ScoreScope,
    audit_status: AuditStatus = "completed",
    degraded_reason: str | None = None,
) -> ReportQualityMetadata:
    counts = {
        severity: sum(item.severity == severity for item in audit.issues)
        for severity in ("critical", "major", "minor")
    }
    scores = audit.scores.model_dump(mode="json")
    scores.pop("total", None)
    return ReportQualityMetadata(
        report_version=report_version,
        audit_status=audit_status,
        quality_score=audit.scores.total,
        quality_score_scope=score_scope,
        quality_passed=audit.passed,
        component_scores=scores,
        issue_counts=counts,
        unresolved_issue_ids=tuple(audit.unresolved_issue_ids),
        degraded_reason=degraded_reason,
    )
