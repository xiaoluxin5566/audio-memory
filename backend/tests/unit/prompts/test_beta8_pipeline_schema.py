from copy import deepcopy
from typing import get_args

import pytest
from pydantic import ValidationError

from audio_memory.prompts.beta8_pipeline_schema import (
    Beta8AuditIssue,
    Beta8OrchestrationResult,
    Beta8PublicationBundle,
    Beta8SearchPacket,
    validate_orchestration_against_inputs,
    validate_publication_bundle,
)


def test_audit_supports_all_report_quality_issue_types() -> None:
    expected = {
        "wrong_subject",
        "wrong_communication_boundary",
        "low_independent_value",
        "thin_content",
        "dense_unstructured_body",
        "structure_content_mismatch",
        "non_actionable_help",
        "reporting_tone",
        "duplicate_heading_number",
        "source_registry_mismatch",
    }

    assert expected.issubset(get_args(Beta8AuditIssue.model_fields["issue_type"].annotation))


def test_structure_content_mismatch_targets_one_card_and_one_location() -> None:
    issue = Beta8AuditIssue.model_validate({
        "card_id": "card_1",
        "suggested_scene_id": None,
        "issue_type": "structure_content_mismatch",
        "severity": "important",
        "problem": "方案比较仍堆在一个长段落中",
        "required_change": "使用对比表呈现方案差异",
        "affected_excerpt": "方案 A 与方案 B……",
        "target_section": "方案比较",
        "evidence_segment_ids": [],
    })

    assert issue.card_id == "card_1"
    assert issue.target_section == "方案比较"


def test_structure_content_mismatch_rejects_missing_target_section() -> None:
    with pytest.raises(ValidationError, match="target_section"):
        Beta8AuditIssue.model_validate({
            "card_id": "card_1",
            "suggested_scene_id": None,
            "issue_type": "structure_content_mismatch",
            "severity": "important",
            "problem": "方案比较仍堆在一个长段落中",
            "required_change": "使用对比表呈现方案差异",
            "affected_excerpt": "方案 A 与方案 B……",
            "evidence_segment_ids": [],
        })


def test_target_section_is_only_valid_for_card_scoped_issues() -> None:
    with pytest.raises(ValidationError, match="target_section"):
        Beta8AuditIssue.model_validate({
            "card_id": None,
            "suggested_scene_id": "self_growth",
            "issue_type": "missed_high_value_content",
            "severity": "important",
            "problem": "遗漏内容",
            "required_change": "补卡",
            "affected_excerpt": None,
            "target_section": "方案比较",
            "evidence_segment_ids": ["seg-1"],
        })


def valid_orchestration() -> dict:
    return {
        "input_complete": True,
        "input_error": None,
        "audit_issue_decisions": [{
            "audit_issue_ids": ["issue-1"], "decision": "accepted",
            "reason": "证据支持", "revision_task_key": "revision-1",
        }],
        "card_decisions": [{
            "decision_key": "decision-1", "operation": "revise",
            "source_card_ids": ["card-1"], "target_scene_id": "work_communication",
            "reason": "修正归属", "revision_task_key": "revision-1",
        }],
        "revision_tasks": [{
            "revision_task_key": "revision-1", "decision_key": "decision-1",
            "operation": "revise", "target_scene_id": "work_communication",
            "source_card_ids": ["card-1"], "audit_issue_ids": ["issue-1"],
            "requirements": [{
                "requirement_id": "req-1", "instruction": "修正归属",
                "evidence_segment_ids": ["seg-1"],
            }],
            "preserve_points": [], "remove_or_avoid": [],
            "completion_criteria": ["归属正确"], "search_task_keys": ["search-1"],
        }],
        "search_tasks": [{
            "search_task_key": "search-1", "source_candidate_ids": ["candidate-1"],
            "question": "正式文档支持什么？", "purpose": "改善建议",
            "target_decision_keys": ["decision-1"], "related_segment_ids": ["seg-1"],
            "source_requirements": ["官方文档"], "jurisdiction": None,
            "freshness_requirement": None,
        }],
        "dropped_search_candidates": [],
        "todo_candidates": [{
            "source_todo_candidate_ids": ["todo-1"], "text": "确认方案",
            "owner_type": "user", "assignee_text": None, "due_at": None,
            "due_text": None, "evidence_segment_ids": ["seg-1"],
        }],
        "dropped_todo_candidates": [],
        "unresolved_conflicts": [],
    }


def validate(data: dict, *, cards={"card-1"}, issues={"issue-1"}, searches={"candidate-1"}, todos={"todo-1"}) -> None:
    result = Beta8OrchestrationResult.model_validate(data)
    validate_orchestration_against_inputs(
        result, card_ids=set(cards), audit_issue_ids=set(issues),
        search_candidate_ids=set(searches), todo_candidate_ids=set(todos),
    )


def test_keep_decision_has_one_source_and_no_revision_task() -> None:
    data = valid_orchestration()
    data["card_decisions"][0].update(operation="keep", source_card_ids=["card-1", "card-2"], revision_task_key=None)
    with pytest.raises(ValidationError, match="keep"):
        Beta8OrchestrationResult.model_validate(data)


def test_merge_requires_at_least_two_source_cards() -> None:
    data = valid_orchestration()
    data["card_decisions"][0]["operation"] = "merge"
    data["revision_tasks"][0]["operation"] = "merge"
    with pytest.raises(ValidationError, match="merge"):
        Beta8OrchestrationResult.model_validate(data)


def test_create_requires_missed_value_issue() -> None:
    data = valid_orchestration()
    data["card_decisions"][0].update(operation="create", source_card_ids=[])
    data["revision_tasks"][0].update(operation="create", source_card_ids=[])
    with pytest.raises(ValueError, match="missed_high_value_content"):
        validate(data, cards=set())


def test_every_input_card_is_consumed_exactly_once() -> None:
    with pytest.raises(ValueError, match="card IDs"):
        validate(valid_orchestration(), cards={"card-1", "card-2"})


def test_orchestration_cannot_drop_work_communication_card() -> None:
    data = valid_orchestration()
    data["card_decisions"][0].update(operation="drop", revision_task_key=None)
    data["revision_tasks"] = []
    result = Beta8OrchestrationResult.model_validate(data)

    with pytest.raises(ValueError, match="work communication card cannot be dropped"):
        validate_orchestration_against_inputs(
            result,
            card_ids={"card-1"},
            audit_issue_ids={"issue-1"},
            search_candidate_ids={"candidate-1"},
            todo_candidate_ids={"todo-1"},
            work_unit_ids_by_card_id={"card-1": ("comm-1",)},
        )


def test_orchestration_cannot_merge_distinct_work_communication_units() -> None:
    data = valid_orchestration()
    data["card_decisions"][0].update(
        operation="merge",
        source_card_ids=["card-1", "card-2"],
        revision_task_key="revision-1",
    )
    data["revision_tasks"][0].update(
        operation="merge", source_card_ids=["card-1", "card-2"]
    )
    result = Beta8OrchestrationResult.model_validate(data)

    with pytest.raises(ValueError, match="distinct work communication units cannot be merged"):
        validate_orchestration_against_inputs(
            result,
            card_ids={"card-1", "card-2"},
            audit_issue_ids={"issue-1"},
            search_candidate_ids={"candidate-1"},
            todo_candidate_ids={"todo-1"},
            work_unit_ids_by_card_id={
                "card-1": ("comm-1",),
                "card-2": ("comm-2",),
            },
        )


def test_publication_requires_exactly_one_work_card_per_expected_unit() -> None:
    base_card = {
        "scene_id": "work_communication", "position": 0,
        "title": "标题", "summary": "摘要", "markdown": "# 标题\n\n摘要",
        "source_segment_ids": ["seg-1"], "used_source_ids": [],
        "origin_card_ids": ["card-1"], "v1_markdown_sha256": "a",
        "final_markdown_sha256": "a", "untouched": True,
    }
    bundle_data = {
        "cards": [
            {**base_card, "card_id": "final-1", "work_communication_unit_ids": ["comm-1"]},
            {**base_card, "card_id": "final-2", "position": 1,
             "origin_card_ids": ["card-2"], "work_communication_unit_ids": ["comm-2"]},
        ],
        "todo_candidates": [], "external_sources": [],
        "v1_card_hashes": {"final-1": "a", "final-2": "a"},
        "final_card_hashes": {"final-1": "a", "final-2": "a"},
        "untouched_card_ids": ["final-1", "final-2"],
        "completed_revision_task_ids": [], "search_degraded": False,
        "search_degraded_reason": None,
        "expected_work_communication_unit_ids": ["comm-1", "comm-2"],
    }

    validate_publication_bundle(Beta8PublicationBundle.model_validate(bundle_data))

    broken = deepcopy(bundle_data)
    broken["cards"][1]["work_communication_unit_ids"] = ["comm-1"]
    with pytest.raises(ValueError, match="work communication unit closure"):
        validate_publication_bundle(Beta8PublicationBundle.model_validate(broken))


def test_publication_work_communication_ledger_fields_are_required() -> None:
    card = {
        "card_id": "final-1", "scene_id": "work_communication", "position": 0,
        "title": "标题", "summary": "摘要", "markdown": "# 标题\n\n摘要",
        "source_segment_ids": ["seg-1"], "used_source_ids": [],
        "origin_card_ids": ["card-1"], "v1_markdown_sha256": "a",
        "final_markdown_sha256": "a", "untouched": True,
    }
    common = {
        "todo_candidates": [], "external_sources": [],
        "v1_card_hashes": {"final-1": "a"},
        "final_card_hashes": {"final-1": "a"},
        "untouched_card_ids": ["final-1"], "completed_revision_task_ids": [],
        "search_degraded": False, "search_degraded_reason": None,
    }

    with pytest.raises(ValidationError, match="work_communication_unit_ids"):
        Beta8PublicationBundle.model_validate({
            **common, "cards": [card],
            "expected_work_communication_unit_ids": [],
        })
    with pytest.raises(ValidationError, match="expected_work_communication_unit_ids"):
        Beta8PublicationBundle.model_validate({
            **common,
            "cards": [{**card, "work_communication_unit_ids": []}],
        })


def test_every_audit_issue_has_exactly_one_decision() -> None:
    data = valid_orchestration()
    data["audit_issue_decisions"].append(deepcopy(data["audit_issue_decisions"][0]))
    with pytest.raises(ValueError, match="audit issue IDs"):
        validate(data)


def test_every_todo_candidate_is_kept_or_dropped_once() -> None:
    data = valid_orchestration()
    data["dropped_todo_candidates"] = [{"source_todo_candidate_ids": ["todo-1"], "reason": "重复"}]
    with pytest.raises(ValueError, match="todo candidate IDs"):
        validate(data)


def test_revision_requirement_partition_is_exact() -> None:
    from audio_memory.prompts.beta8_pipeline_schema import Beta8RevisedCard

    card = Beta8RevisedCard.model_validate({
        "input_complete": True, "input_error": None, "reserved_card_id": "final-1",
        "operation": "revise", "markdown": "# 标题\n\n摘要",
        "source_segment_ids": ["seg-1"], "used_source_ids": [], "todo_candidates": [],
        "completed_requirement_ids": ["req-1"], "unresolved_requirement_ids": ["req-1"],
    })
    with pytest.raises(ValueError, match="requirement IDs"):
        card.validate_requirement_ids({"req-1"})


def test_failed_search_packet_has_no_sources_or_answer() -> None:
    with pytest.raises(ValidationError, match="failed"):
        Beta8SearchPacket.model_validate({
            "search_task_id": "task-1", "status": "failed", "answer": "不应存在",
            "supported_points": [], "unresolved_points": [], "sources": [],
            "error_summary": "失败",
        })


def test_publish_bundle_rejects_changed_untouched_card_hash() -> None:
    bundle = Beta8PublicationBundle.model_validate({
        "cards": [{
            "card_id": "final-1", "scene_id": "work_communication", "position": 0,
            "title": "标题", "summary": "摘要", "markdown": "# 标题\n\n摘要",
            "source_segment_ids": ["seg-1"], "used_source_ids": [],
            "origin_card_ids": ["card-1"], "v1_markdown_sha256": "a",
            "final_markdown_sha256": "b", "untouched": True,
            "work_communication_unit_ids": [],
        }],
        "todo_candidates": [], "external_sources": [], "v1_card_hashes": {"final-1": "a"},
        "final_card_hashes": {"final-1": "b"}, "untouched_card_ids": ["final-1"],
        "completed_revision_task_ids": [], "search_degraded": False,
        "search_degraded_reason": None,
        "expected_work_communication_unit_ids": [],
    })
    with pytest.raises(ValueError, match="untouched"):
        validate_publication_bundle(bundle)
