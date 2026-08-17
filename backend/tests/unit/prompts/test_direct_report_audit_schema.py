from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_memory.prompts.direct_report_audit_schema import ReportAudit


def audit_payload(*, mode: str = "full_v1_audit") -> dict[str, object]:
    return {
        "audit_mode": mode,
        "rubric_version": 1,
        "passed": True,
        "scores": {
            "factual_accuracy": 27,
            "important_coverage": 22,
            "analysis_depth": 17,
            "actionability": 12,
            "expression_structure": 9,
            "total": 87,
        },
        "deductions": [
            {
                "dimension": "factual_accuracy",
                "points": 3,
                "reason": "One entity remains uncertain.",
            }
        ],
        "coverage": {
            "full_transcript_reviewed": mode == "full_v1_audit",
            "reviewed_segment_count": 10 if mode == "full_v1_audit" else None,
            "total_segment_count": 10 if mode == "full_v1_audit" else None,
            "unreviewed_ranges": [],
            "summary": "All supplied transcript ranges reviewed."
            if mode == "full_v1_audit"
            else "Bounded revision evidence reviewed.",
        },
        "issues": [],
        "unresolved_issue_ids": [],
        "summary": "The report is publishable.",
    }


def material_issue(*, severity: str = "major") -> dict[str, object]:
    return {
        "issue_id": "issue_001",
        "severity": severity,
        "issue_type": "factual_error",
        "section_id": "section_002",
        "problem": "The report confuses joining a startup with founding one.",
        "importance": "It changes the central career decision.",
        "required_change": "State that the speaker considered joining a startup.",
        "affected_claims": ["The reader plans to found a company."],
        "evidence_segment_ids": ["seg_0_1"],
        "evidence_excerpts": [
            {"segment_id": "seg_0_1", "text": "I may join an early startup."}
        ],
        "context_excerpts": [],
        "allow_deletion_or_compression": False,
    }


def test_audit_rejects_score_total_that_does_not_equal_dimensions() -> None:
    payload = audit_payload()
    payload["scores"]["total"] = 88  # type: ignore[index]

    with pytest.raises(ValidationError, match="sum"):
        ReportAudit.model_validate(payload)


def test_audit_rejects_high_score_when_critical_issue_is_unresolved() -> None:
    payload = audit_payload()
    payload["issues"] = [material_issue(severity="critical")]
    payload["unresolved_issue_ids"] = ["issue_001"]
    payload["passed"] = False

    with pytest.raises(ValidationError, match="59"):
        ReportAudit.model_validate(payload)


def test_full_audit_requires_complete_numeric_coverage() -> None:
    payload = audit_payload()
    payload["coverage"]["reviewed_segment_count"] = 9  # type: ignore[index]

    with pytest.raises(ValidationError, match="coverage"):
        ReportAudit.model_validate(payload)


def test_chunk_audit_covers_current_chunk_without_claiming_full_transcript() -> None:
    payload = audit_payload(mode="chunk_v1_audit")
    payload["coverage"]["full_transcript_reviewed"] = False  # type: ignore[index]
    payload["coverage"]["reviewed_segment_count"] = 12  # type: ignore[index]
    payload["coverage"]["total_segment_count"] = 12  # type: ignore[index]

    audit = ReportAudit.model_validate(payload)

    assert audit.coverage.full_transcript_reviewed is False


def test_audit_uses_safe_internal_summary_when_model_omits_it() -> None:
    payload = audit_payload(mode="chunk_v1_audit")
    payload["coverage"].update({  # type: ignore[union-attr]
        "full_transcript_reviewed": False,
        "reviewed_segment_count": 12,
        "total_segment_count": 12,
    })
    payload.pop("summary")

    audit = ReportAudit.model_validate(payload)

    assert audit.summary == "审核完成。"


def test_audit_normalizes_stale_unresolved_ids_to_returned_issues() -> None:
    payload = audit_payload(mode="revision_final_audit")
    payload["issues"] = [material_issue()]
    payload["unresolved_issue_ids"] = ["old_issue_id"]
    payload["passed"] = False
    payload["scores"]["total"] = 69  # type: ignore[index]
    payload["scores"]["factual_accuracy"] = 9  # type: ignore[index]

    audit = ReportAudit.model_validate(payload)

    assert audit.unresolved_issue_ids == ["issue_001"]


def test_final_audit_cannot_claim_full_transcript_review() -> None:
    payload = audit_payload(mode="revision_final_audit")
    payload["coverage"]["full_transcript_reviewed"] = True  # type: ignore[index]

    with pytest.raises(ValidationError, match="bounded"):
        ReportAudit.model_validate(payload)


def test_material_issue_requires_evidence_packet() -> None:
    payload = audit_payload()
    issue = material_issue()
    issue["evidence_excerpts"] = []
    payload["issues"] = [issue]
    payload["passed"] = False

    with pytest.raises(ValidationError, match="evidence"):
        ReportAudit.model_validate(payload)


def test_every_issue_requires_a_target_section_for_revision() -> None:
    payload = audit_payload()
    issue = material_issue(severity="minor")
    issue["section_id"] = None
    issue["evidence_segment_ids"] = []
    issue["evidence_excerpts"] = []
    payload["issues"] = [issue]
    payload["unresolved_issue_ids"] = ["issue_001"]

    with pytest.raises(ValidationError, match="target section"):
        ReportAudit.model_validate(payload)


def test_audit_accepts_section_local_value_opportunity() -> None:
    payload = audit_payload()
    payload["value_opportunities"] = [{
        "opportunity_id": "opportunity_knowledge_selected_topic",
        "kind": "knowledge_enrichment",
        "section_id": "section_004",
        "current_gap": "只复述内容，没有解释概念。",
        "desired_value": "解释概念、机制和判断边界。",
        "evidence_segment_ids": ["seg_0_1"],
        "evidence_excerpts": [
            {"segment_id": "seg_0_1", "text": "九蒸九晒黄精。"}
        ],
        "preserve_constraints": ["不得推断读者身体状况"],
        "allow_section_rewrite": True,
    }]

    audit = ReportAudit.model_validate(payload)

    assert audit.value_opportunities[0].kind == "knowledge_enrichment"


def test_audit_rejects_duplicate_value_opportunity_ids() -> None:
    payload = audit_payload()
    opportunity = {
        "opportunity_id": "opportunity_duplicate",
        "kind": "analysis_deepening",
        "section_id": "section_002",
        "current_gap": "只复述事实。",
        "desired_value": "解释关键取舍。",
        "evidence_segment_ids": [],
        "evidence_excerpts": [],
        "preserve_constraints": [],
        "allow_section_rewrite": False,
    }
    payload["value_opportunities"] = [opportunity, dict(opportunity)]

    with pytest.raises(ValidationError, match="opportunity IDs"):
        ReportAudit.model_validate(payload)
