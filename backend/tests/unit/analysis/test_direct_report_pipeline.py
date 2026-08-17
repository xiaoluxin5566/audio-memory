from __future__ import annotations

import pytest

from audio_memory.analysis import direct_report_pipeline as pipeline
from audio_memory.analysis.direct_report_pipeline import (
    ReportQualityMetadata,
    apply_audited_revision,
    build_section_diffs,
    revision_target_section_ids,
)
from audio_memory.prompts.direct_report_audit_schema import ReportAudit
from audio_memory.prompts.direct_report_revision_schema import TargetedReportRevision


V1 = """# Title

## Overview

Overview content that stays unchanged.

## Work

The reader plans to found a startup. This paragraph contains useful context that must remain.

## Boundary

Identity remains uncertain.
"""


def audit() -> ReportAudit:
    return ReportAudit.model_validate({
        "audit_mode": "full_v1_audit",
        "rubric_version": 1,
        "passed": False,
        "scores": {
            "factual_accuracy": 20,
            "important_coverage": 20,
            "analysis_depth": 15,
            "actionability": 9,
            "expression_structure": 5,
            "total": 69,
        },
        "deductions": [{"dimension": "factual_accuracy", "points": 10, "reason": "Wrong career decision."}],
        "coverage": {
            "full_transcript_reviewed": True,
            "reviewed_segment_count": 1,
            "total_segment_count": 1,
            "unreviewed_ranges": [],
            "summary": "Complete.",
        },
        "issues": [{
            "issue_id": "issue_001",
            "severity": "major",
            "issue_type": "factual_error",
            "section_id": "section_002",
            "problem": "Founding and joining are confused.",
            "importance": "Core decision is wrong.",
            "required_change": "Say joining.",
            "affected_claims": ["plans to found"],
            "evidence_segment_ids": ["seg_0_1"],
            "evidence_excerpts": [{"segment_id": "seg_0_1", "text": "I may join a startup."}],
            "context_excerpts": [],
            "allow_deletion_or_compression": False,
        }],
        "unresolved_issue_ids": ["issue_001"],
        "summary": "Revision required.",
    })


def revision(*, evidence_ids: list[str] | None = None) -> TargetedReportRevision:
    return TargetedReportRevision.model_validate({
        "revisions": [{
            "section_id": "section_002",
            "title": "Work",
            "revised_markdown": "## Work\n\nThe reader is considering joining a startup. This paragraph contains useful context that must remain.",
            "issues_resolved": ["issue_001"],
            "evidence_segment_ids": evidence_ids or ["seg_0_1"],
            "removes_repetition": False,
            "repetition_reason": None,
        }],
        "unresolved_issue_ids": [],
        "revision_summary": "Corrected the decision.",
    })


def test_apply_audited_revision_preserves_untouched_sections_and_builds_diff() -> None:
    v2 = apply_audited_revision(V1, audit(), revision(), {"seg_0_1"})

    assert "considering joining a startup" in v2
    assert "Overview content that stays unchanged" in v2
    assert "Identity remains uncertain" in v2
    assert build_section_diffs(V1, v2) == [{
        "section_id": "section_002",
        "title": "Work",
        "before": "## Work\n\nThe reader plans to found a startup. This paragraph contains useful context that must remain.\n\n",
        "after": "## Work\n\nThe reader is considering joining a startup. This paragraph contains useful context that must remain.\n\n",
    }]


def test_apply_audited_revision_rejects_evidence_outside_issue_packet() -> None:
    with pytest.raises(ValueError, match="audit evidence"):
        apply_audited_revision(V1, audit(), revision(evidence_ids=["seg_9_9"]), {"seg_0_1", "seg_9_9"})


def test_apply_audited_revision_requires_every_material_issue_accounted_for() -> None:
    payload = revision().model_dump(mode="json")
    payload["revisions"] = []
    incomplete = TargetedReportRevision.model_validate(payload)

    with pytest.raises(ValueError, match="unaccounted"):
        apply_audited_revision(V1, audit(), incomplete, {"seg_0_1"})


def test_apply_audited_revision_rejects_resolved_claim_left_in_target_section() -> None:
    payload = revision().model_dump(mode="json")
    payload["revisions"][0]["revised_markdown"] = (
        "## Work\n\nThe reader still plans to found a startup. "
        "This paragraph contains useful context that must remain."
    )
    unchanged_claim = TargetedReportRevision.model_validate(payload)

    with pytest.raises(ValueError, match="affected claim remains"):
        apply_audited_revision(V1, audit(), unchanged_claim, {"seg_0_1"})


def test_apply_revision_allows_section_title_fix_when_audit_targets_heading() -> None:
    payload = audit().model_dump(mode="json")
    payload["issues"][0].update({
        "section_id": "section_003",
        "issue_type": "uncertain_attribution",
        "problem": "The section title states an uncertain identity as fact.",
        "required_change": "Revise the title to preserve uncertainty.",
        "affected_claims": [],
        "evidence_segment_ids": ["report_section_003"],
        "evidence_excerpts": [{
            "segment_id": "report_section_003",
            "text": "## Boundary",
        }],
    })
    title_audit = ReportAudit.model_validate(payload)
    revision_payload = revision().model_dump(mode="json")
    revision_payload["revisions"][0].update({
        "section_id": "section_003",
        "title": "Boundary remains uncertain",
        "revised_markdown": "## Boundary remains uncertain\n\nIdentity remains uncertain.",
        "evidence_segment_ids": [],
    })
    title_revision = TargetedReportRevision.model_validate(revision_payload)

    v2 = apply_audited_revision(V1, title_audit, title_revision, {"seg_0_1"})

    assert "## Boundary remains uncertain" in v2


def test_quality_metadata_rejects_score_without_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        ReportQualityMetadata(
            report_version="v1",
            audit_status="completed",
            quality_score=80,
            quality_score_scope=None,
            quality_passed=True,
        )


def test_revision_targets_primary_and_related_issue_sections() -> None:
    payload = audit().model_dump(mode="json")
    payload["issues"][0]["related_section_ids"] = ["section_003"]
    multi_section_audit = ReportAudit.model_validate(payload)

    assert revision_target_section_ids(V1, multi_section_audit) == {
        "section_002", "section_003"
    }


def test_revision_targets_value_opportunity_section_and_evidence() -> None:
    payload = audit().model_dump(mode="json")
    payload["value_opportunities"] = [{
        "opportunity_id": "opportunity_framework",
        "kind": "analysis_deepening",
        "section_id": "section_003",
        "current_gap": "Only states the uncertainty.",
        "desired_value": "Explain a reusable decision boundary.",
        "evidence_segment_ids": ["seg_0_2"],
        "evidence_excerpts": [{
            "segment_id": "seg_0_2",
            "text": "I need a boundary for deciding.",
        }],
        "preserve_constraints": ["Keep the uncertainty explicit."],
        "allow_section_rewrite": True,
    }]
    opportunity_audit = ReportAudit.model_validate(payload)

    assert revision_target_section_ids(V1, opportunity_audit) == {
        "section_002", "section_003"
    }
    assert pipeline.audit_transcript_evidence_ids(
        opportunity_audit, {"seg_0_1", "seg_0_2"}
    ) == {"seg_0_1", "seg_0_2"}


def test_apply_revision_validates_value_opportunity_section() -> None:
    payload = audit().model_dump(mode="json")
    payload["value_opportunities"] = [{
        "opportunity_id": "opportunity_framework",
        "kind": "analysis_deepening",
        "section_id": "section_003",
        "current_gap": "Only states the uncertainty.",
        "desired_value": "Explain a reusable decision boundary.",
        "evidence_segment_ids": ["seg_0_2"],
        "evidence_excerpts": [{
            "segment_id": "seg_0_2",
            "text": "I need a boundary for deciding.",
        }],
        "preserve_constraints": [],
        "allow_section_rewrite": True,
    }]
    opportunity_audit = ReportAudit.model_validate(payload)
    revision_payload = revision().model_dump(mode="json")
    revision_payload["revisions"][0]["opportunities_resolved"] = [
        "opportunity_framework"
    ]
    revision_payload["revisions"][0]["evidence_segment_ids"].append("seg_0_2")
    wrong_section = TargetedReportRevision.model_validate(revision_payload)

    with pytest.raises(ValueError, match="opportunity section"):
        apply_audited_revision(
            V1,
            opportunity_audit,
            wrong_section,
            {"seg_0_1", "seg_0_2"},
        )


def report_expression_audit() -> ReportAudit:
    payload = audit().model_dump(mode="json")
    payload["issues"] = [{
        "issue_id": "issue_process_leak",
        "severity": "major",
        "issue_type": "internal_process_leak",
        "section_id": "section_003",
        "problem": "The boundary section exposes internal processing.",
        "importance": "It is not useful to the reader.",
        "required_change": "Delete the internal process explanation.",
        "affected_claims": ["Identity remains uncertain."],
        "evidence_segment_ids": ["report_section_003"],
        "evidence_excerpts": [{
            "segment_id": "report_section_003",
            "text": "Identity remains uncertain.",
        }],
        "context_excerpts": [],
        "allow_deletion_or_compression": True,
    }]
    payload["unresolved_issue_ids"] = ["issue_process_leak"]
    return ReportAudit.model_validate(payload)


def test_validate_audit_evidence_accepts_report_section_evidence() -> None:
    pipeline.validate_audit_evidence(
        report_expression_audit(),
        transcript_by_id={"seg_0_1": "I may join a startup."},
        report_markdown=V1,
    )


def test_validate_audit_evidence_rejects_non_verbatim_report_excerpt() -> None:
    payload = report_expression_audit().model_dump(mode="json")
    payload["issues"][0]["evidence_excerpts"][0]["text"] = "Not in report."
    invalid = ReportAudit.model_validate(payload)

    with pytest.raises(ValueError, match="report section"):
        pipeline.validate_audit_evidence(
            invalid,
            transcript_by_id={"seg_0_1": "I may join a startup."},
            report_markdown=V1,
        )


def test_revision_allows_only_transcript_evidence_ids() -> None:
    assert pipeline.audit_transcript_evidence_ids(
        report_expression_audit(), {"seg_0_1"}
    ) == set()


def test_canonicalize_audit_evidence_repairs_near_verbatim_transcript_excerpt() -> None:
    payload = audit().model_dump(mode="json")
    payload["issues"][0]["evidence_excerpts"][0]["text"] = (
        "I may startup join."
    )
    near_verbatim = ReportAudit.model_validate(payload)

    repaired = pipeline.canonicalize_audit_evidence(
        near_verbatim, {"seg_0_1": "I may join a startup."}
    )

    assert repaired.issues[0].evidence_excerpts[0].text == "I may join a startup."


def test_canonicalize_audit_evidence_rejects_unrelated_excerpt() -> None:
    payload = audit().model_dump(mode="json")
    payload["issues"][0]["evidence_excerpts"][0]["text"] = "Entirely unrelated."
    unrelated = ReportAudit.model_validate(payload)

    with pytest.raises(ValueError, match="not near-verbatim"):
        pipeline.canonicalize_audit_evidence(
            unrelated, {"seg_0_1": "I may join a startup."}
        )


def test_sanitize_audit_evidence_drops_bad_excerpt_but_keeps_supported_issue() -> None:
    payload = audit().model_dump(mode="json")
    payload["issues"][0]["evidence_segment_ids"] = ["seg_0_0", "seg_0_1"]
    payload["issues"][0]["evidence_excerpts"] = [
        {"segment_id": "seg_0_0", "text": "completely invented"},
        {"segment_id": "seg_0_1", "text": "真实原句"},
    ]

    sanitized = pipeline.sanitize_audit_evidence(
        ReportAudit.model_validate(payload),
        {"seg_0_0": "另一句话", "seg_0_1": "真实原句"},
    )

    assert sanitized.issues[0].evidence_segment_ids == ["seg_0_1"]
    assert [item.segment_id for item in sanitized.issues[0].evidence_excerpts] == [
        "seg_0_1"
    ]


def test_canonicalize_audit_evidence_replaces_joined_report_excerpt() -> None:
    payload = report_expression_audit().model_dump(mode="json")
    payload["issues"][0]["evidence_excerpts"][0]["text"] = (
        "Identity remains uncertain. Not contiguous."
    )
    joined = ReportAudit.model_validate(payload)

    repaired = pipeline.canonicalize_audit_evidence(
        joined,
        {"seg_0_1": "I may join a startup."},
        report_markdown=V1,
    )

    assert repaired.issues[0].evidence_excerpts[0].text == (
        "## Boundary\n\nIdentity remains uncertain.\n"
    )
