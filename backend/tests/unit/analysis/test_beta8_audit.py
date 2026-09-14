from copy import deepcopy

import pytest

from audio_memory.analysis.beta8_audit import (
    aggregate_audit_results,
    plan_audit_units,
)
from audio_memory.prompts.beta8_pipeline_schema import Beta8AuditResult


def segment(index: int, text: str = "x" * 50) -> dict[str, object]:
    return {
        "segment_id": f"seg-{index}", "file_id": "file-1", "file_name": "day.mp3",
        "recording_started_at": None, "timezone": "Asia/Shanghai",
        "start_ms": index * 1_000, "end_ms": (index + 1) * 1_000,
        "speaker_id": "unknown", "text": text,
    }


def result(unit, issues=()) -> Beta8AuditResult:
    return Beta8AuditResult.model_validate({
        "audit_phase": unit.phase, "audit_scope": unit.scope,
        "audit_unit_id": unit.audit_unit_id, "input_complete": True,
        "input_error": None, "issues": list(issues), "revision_task_checks": [],
        "passed": not issues,
    })


def issue(excerpt: str = "原文", segment_id: str = "seg-0") -> dict:
    return {
        "card_id": "card-1", "suggested_scene_id": None,
        "issue_type": "wrong_attribution", "severity": "blocking",
        "problem": "归属错误", "required_change": "恢复提出者",
        "affected_excerpt": excerpt, "evidence_segment_ids": [segment_id],
    }


def test_plan_audit_units_covers_every_segment_exactly_once() -> None:
    transcript = [segment(index) for index in range(8)]
    units = plan_audit_units(transcript, phase="initial", max_markdown_chars=240)
    evidence = [unit for unit in units if unit.scope == "evidence_chunk"]
    assert [item["segment_id"] for unit in evidence for item in unit.transcript_segments] == [
        f"seg-{index}" for index in range(8)
    ]


def test_plan_audit_units_adds_one_global_report_unit() -> None:
    units = plan_audit_units([segment(0)], phase="final", max_markdown_chars=240)
    assert [unit.audit_unit_id for unit in units if unit.scope == "global_report"] == [
        "beta8:final:global"
    ]


def test_global_unit_contains_all_cards_and_no_transcript_claim() -> None:
    unit = plan_audit_units([segment(0)], phase="initial", max_markdown_chars=240)[-1]
    assert unit.scope == "global_report"
    assert unit.transcript_segments == ()


def test_aggregate_requires_every_expected_unit() -> None:
    units = plan_audit_units([segment(0)], phase="initial", max_markdown_chars=240)
    with pytest.raises(ValueError, match="Missing audit units"):
        aggregate_audit_results(
            units, [result(units[0])], known_card_ids={"card-1"}
        )


def test_aggregate_rejects_wrong_unit_id() -> None:
    units = plan_audit_units([segment(0)], phase="initial", max_markdown_chars=240)
    wrong = result(units[0]).model_copy(update={"audit_unit_id": "wrong"})
    with pytest.raises(ValueError, match="Unexpected audit units"):
        aggregate_audit_results(
            units, [wrong, result(units[1])], known_card_ids={"card-1"}
        )


def test_aggregate_only_deduplicates_byte_identical_issues() -> None:
    units = plan_audit_units([segment(0), segment(1)], phase="initial", max_markdown_chars=80)
    aggregated = aggregate_audit_results(
        units,
        [
            result(units[0], [issue(), deepcopy(issue())]),
            result(units[1]),
            result(units[2]),
        ],
        known_card_ids={"card-1"},
    )
    assert len(aggregated.issues) == 1
    assert aggregated.issues[0].origin_audit_unit_ids == [
        units[0].audit_unit_id,
    ]


def test_semantically_similar_issues_both_survive_for_orchestration() -> None:
    units = plan_audit_units([segment(0), segment(1)], phase="initial", max_markdown_chars=80)
    aggregated = aggregate_audit_results(
        units,
        [
            result(units[0], [issue("你已决定")]),
            result(units[1], [issue("已经采纳方案", "seg-1")]),
            result(units[2]),
        ],
        known_card_ids={"card-1"},
    )
    assert [item.affected_excerpt for item in aggregated.issues] == [
        "你已决定", "已经采纳方案",
    ]


def test_aggregate_preserves_quality_issue_target_section() -> None:
    units = plan_audit_units([segment(0)], phase="initial", max_markdown_chars=240)
    quality_issue = issue()
    quality_issue.update({
        "issue_type": "structure_content_mismatch",
        "problem": "方案比较仍堆在一个长段落中",
        "required_change": "改成对比表",
        "target_section": "方案比较",
    })

    aggregated = aggregate_audit_results(
        units,
        [result(units[0], [quality_issue]), result(units[1])],
        known_card_ids={"card-1"},
    )

    assert aggregated.issues[0].target_section == "方案比较"


def test_aggregate_rejects_structure_mismatch_for_unknown_card() -> None:
    units = plan_audit_units([segment(0)], phase="initial", max_markdown_chars=240)
    quality_issue = issue()
    quality_issue.update({
        "card_id": "unknown-card",
        "issue_type": "structure_content_mismatch",
        "problem": "方案比较仍堆在一个长段落中",
        "required_change": "改成对比表",
        "target_section": "方案比较",
    })

    with pytest.raises(ValueError, match="unknown card IDs: unknown-card"):
        aggregate_audit_results(
            units,
            [result(units[0], [quality_issue]), result(units[1])],
            known_card_ids={"card-1"},
        )
