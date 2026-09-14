from audio_memory.analysis.beta8_runner import _normalize_audit_result_payload
from audio_memory.prompts.beta8_pipeline_schema import Beta8AuditResult


def test_non_missing_issue_discards_redundant_suggested_scene_id() -> None:
    payload = {
        "audit_phase": "final",
        "audit_scope": "global_report",
        "audit_unit_id": "audit-1",
        "input_complete": True,
        "input_error": None,
        "issues": [{
            "card_id": "final-card-1",
            "suggested_scene_id": "self_growth",
            "issue_type": "thin_content",
            "severity": "important",
            "problem": "内容偏薄",
            "required_change": "补充已有证据",
            "affected_excerpt": None,
            "target_section": None,
            "evidence_segment_ids": ["seg_1_1"],
        }],
        "revision_task_checks": [],
        "passed": False,
    }

    normalized = _normalize_audit_result_payload(payload)

    result = Beta8AuditResult.model_validate(normalized)
    assert result.issues[0].suggested_scene_id is None
    assert payload["issues"][0]["suggested_scene_id"] == "self_growth"


def test_missing_content_issue_keeps_required_suggested_scene_id() -> None:
    payload = {
        "issues": [{
            "card_id": None,
            "suggested_scene_id": "self_growth",
            "issue_type": "missed_high_value_content",
        }],
    }

    normalized = _normalize_audit_result_payload(payload)

    assert normalized == payload
