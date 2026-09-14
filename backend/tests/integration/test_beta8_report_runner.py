from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import re
from types import SimpleNamespace

import pytest

from audio_memory.analysis import beta8_runner as beta8_runner_module
from audio_memory.analysis.beta8_event_index import EventIndexCoverageError
from audio_memory.analysis.beta8_runner import (
    Beta8CheckpointPause,
    Beta8ReportRunner,
    build_strict_source_registry,
)
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.analysis.beta8_search import Beta8SearchExecutor
from audio_memory.analysis.pipeline_identity import build_pipeline_parameters
from audio_memory.analysis.beta8_state import (
    BETA8_STAGE_KEYS,
    Beta8StageRecord,
    canonical_hash,
    validate_stage_record,
)
from audio_memory.analysis.publisher import AnalysisOutcome
from audio_memory.analysis.runner import LeaseLostError
from audio_memory.providers.adapters.base import NativeSearchCallResult
from audio_memory.prompts.day_map_schema import ExternalSource
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript
from audio_memory.prompts.beta8_composer import Beta8PromptComposer, SCENE_IDS
from audio_memory.prompts.beta8_scene_schema import Beta8UnifiedV1Result
from audio_memory.prompts.beta8_pipeline_schema import Beta8SearchPacket


def event_index_payload(*, gap: bool = False) -> dict[str, object]:
    return {
        "input_complete": True,
        "input_error": None,
        "primary_sessions": [] if gap else [{
            "session_id": "communication-1",
            "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "contact_started",
            "start_boundary_evidence_segment_ids": ["seg_0_0"],
            "end_boundary": "contact_ended",
            "end_boundary_evidence_segment_ids": ["seg_0_0"],
            "subject": "AI 硬件讨论",
            "description": "讨论近期关注的 AI 硬件",
        }],
        "embedded_events": [],
        "file_timelines": [] if gap else [{
            "source_file": "file-1",
            "blocks": [{
                "start_segment_id": "seg_0_0",
                "end_segment_id": "seg_0_0",
                "disposition": "session",
                "session_id": "communication-1",
            }],
        }],
    }


def unified_v1_payload() -> dict[str, object]:
    scene_results = []
    for scene_id in SCENE_IDS:
        cards = []
        if scene_id != "life_decisions":
            cards = [{
                "draft_card_key": f"{scene_id}-1",
                "card_basis": {
                    "type": (
                        "work_communication"
                        if scene_id == "work_communication"
                        else "independent_value"
                    ),
                    "source_unit_ids": ["communication-1"],
                    "communication_kind": (
                        "meeting" if scene_id == "work_communication" else None
                    ),
                },
                "markdown": (
                    f"# {scene_id}\n\n{scene_id} 的核心摘要。\n\n"
                    "## 关键内容\n\n这里保留有证据支持的完整内容。"
                ),
                "source_segment_ids": ["seg_0_0"],
                "search_candidates": [],
            }]
        scene_results.append({
            "scene_id": scene_id,
            "cards": cards,
            "todo_candidates": [],
            "skip_reason": None if cards else "无决策内容",
        })
    return {
        "input_complete": True,
        "input_error": None,
        "scene_results": scene_results,
        "omitted_units": [],
    }


def request_payload(call: dict[str, object]) -> dict[str, object]:
    user = str(call["user"])
    body = user[user.index(">") + 1 : user.rindex("</")]
    return json.loads(body)


def test_unified_v1_parser_moves_only_card_level_todos_to_their_scene() -> None:
    payload = unified_v1_payload()
    scene = payload["scene_results"][0]
    first_card = scene["cards"][0]
    nested_todo = {
        "text": "下午拉会确认开关",
        "owner_type": "user",
        "assignee_text": None,
        "due_at": None,
        "due_text": "下午",
        "evidence_segment_ids": ["seg_0_0"],
    }
    first_card["todo_candidates"] = [nested_todo]

    result = Beta8ReportRunner._parse_unified_v1(json.dumps(payload))

    assert result.scene_results[0].todo_candidates[0].model_dump(mode="json") == nested_todo
    assert "todo_candidates" not in result.scene_results[0].cards[0].model_dump(mode="json")


def test_unified_v1_parser_defaults_only_missing_work_communication_kind() -> None:
    payload = unified_v1_payload()
    work_basis = payload["scene_results"][0]["cards"][0]["card_basis"]
    work_basis["communication_kind"] = None
    independent_basis = payload["scene_results"][1]["cards"][0]["card_basis"]
    assert independent_basis["communication_kind"] is None

    result = Beta8ReportRunner._parse_unified_v1(json.dumps(payload))

    assert result.scene_results[0].cards[0].card_basis.communication_kind == "other"
    assert result.scene_results[1].cards[0].card_basis.communication_kind is None


class GenerationSource:
    async def credential_generation(self, provider_id: str) -> int:
        return 1


class Publisher:
    def __init__(self) -> None:
        self.bundles = []

    async def publish_beta8(self, version_id, bundle, *, worker_owner_id=None):
        self.bundles.append(bundle)
        return AnalysisOutcome(batch_id="batch-1", card_count=len(bundle.cards), todo_count=0)


class Provider:
    def __init__(self, *, block_scenes=False, block_audits=False, fail_scene=None, fail_any_scene_once=None, final_issue=False, final_issue_type=None, incomplete_revision=False, revise=False, invalid_scene_once=None, misindexed_scene_once=None, truncate_scene_once=None, truncate_event_index_with_partial_once=False, native_usage_once=None, audit_needs_large_budget=False, invalid_audit_once=False, orchestration_needs_large_budget=False, orchestration_needs_convergence=False, invalid_orchestration_once=False, invalid_orchestration_twice=False, invalid_orchestration_json_once=False, invalid_revision_once=False, invalid_revision_markdown_once=False, index_gap_failures=0, index_overlap_once=False, invalid_index_schema_once=False, invalid_v1_schema_failures=0, invalid_v1_json_once=False, localizable_v1_json_once=False, unsafe_v1_json_once=None, incomplete_v1_once=False, missing_v1_scenes_once=False, unknown_v1_evidence_once=False, invalid_v1_markdown_once=False, v1_error_code=None, token_usage_available=True, retry_scene_once=None, include_search_task=False, leak_internal_id_in_orchestration=False, v1_privacy_leak=None, audit_repair_privacy_leak=False, orchestration_repair_privacy_leak=False, audit_unicode_privacy_leak=False) -> None:
        self.calls = []
        self.block_scenes = block_scenes
        self.block_audits = block_audits
        self.fail_scene = fail_scene
        self.fail_any_scene_once = fail_any_scene_once
        self.final_issue = final_issue
        self.final_issue_type = final_issue_type
        self.incomplete_revision = incomplete_revision
        self.revise = revise
        self.invalid_scene_once = invalid_scene_once
        self.misindexed_scene_once = misindexed_scene_once
        self.truncate_scene_once = truncate_scene_once
        self.truncate_event_index_with_partial_once = truncate_event_index_with_partial_once
        self.native_usage_once = native_usage_once
        self.audit_needs_large_budget = audit_needs_large_budget
        self.invalid_audit_once = invalid_audit_once
        self.orchestration_needs_large_budget = orchestration_needs_large_budget
        self.orchestration_needs_convergence = orchestration_needs_convergence
        self.invalid_orchestration_sequence = (
            ["accepted_without_task", "create_with_sources"]
            if invalid_orchestration_twice
            else (["accepted_without_task"] if invalid_orchestration_once else [])
        )
        self.invalid_orchestration_json_once = invalid_orchestration_json_once
        self.invalid_revision_once = invalid_revision_once
        self.invalid_revision_markdown_once = invalid_revision_markdown_once
        self.index_gap_failures = index_gap_failures
        self.index_overlap_once = index_overlap_once
        self.invalid_index_schema_once = invalid_index_schema_once
        self.invalid_v1_schema_failures = invalid_v1_schema_failures
        self.invalid_v1_json_once = invalid_v1_json_once
        self.localizable_v1_json_once = localizable_v1_json_once
        self.unsafe_v1_json_once = unsafe_v1_json_once
        self.incomplete_v1_once = incomplete_v1_once
        self.missing_v1_scenes_once = missing_v1_scenes_once
        self.unknown_v1_evidence_once = unknown_v1_evidence_once
        self.invalid_v1_markdown_once = invalid_v1_markdown_once
        self.v1_error_code = v1_error_code
        self.token_usage_available = token_usage_available
        self.retry_scene_once = retry_scene_once
        self.include_search_task = include_search_task
        self.leak_internal_id_in_orchestration = leak_internal_id_in_orchestration
        self.v1_privacy_leak = v1_privacy_leak
        self.audit_repair_privacy_leak = audit_repair_privacy_leak
        self.orchestration_repair_privacy_leak = orchestration_repair_privacy_leak
        self.audit_unicode_privacy_leak = audit_unicode_privacy_leak
        self.native_search_calls = []
        self.native_search_usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "token_usage_unavailable_reason": None,
            "response_count": 0,
            "tool_call_count": 0,
        }
        self.request_diagnostics = []
        self.started = []
        self.release = asyncio.Event()
        self.audit_release = asyncio.Event()
        self.audit_started = 0

    async def generate(self, provider_id, **kwargs):
        invocation_id = kwargs["diagnostic_invocation_id"]
        self.request_diagnostics.append(SimpleNamespace(
            invocation_id=invocation_id,
            attempt_index=0,
            provider_id=provider_id,
            model_id=kwargs["model_id"],
            scene_id=kwargs["scene_id"],
            input_tokens=100 if self.token_usage_available else None,
            output_tokens=20 if self.token_usage_available else None,
            token_usage_unavailable_reason=(
                None if self.token_usage_available else "provider response omitted usage"
            ),
            elapsed_seconds=0.125,
            started_at="2026-09-04T00:00:00+00:00",
            finished_at="2026-09-04T00:00:00.125000+00:00",
            status_category="2xx",
            repair_attempted=kwargs["repair_attempted"],
        ))
        if self.retry_scene_once == kwargs["scene_id"]:
            self.retry_scene_once = None
            self.request_diagnostics[-1].status_category = "5xx"
            self.request_diagnostics.append(SimpleNamespace(
                **{
                    **vars(self.request_diagnostics[-1]),
                    "attempt_index": 1,
                    "status_category": "2xx",
                }
            ))
        if self.native_usage_once is not None:
            self.native_search_usage_totals.update(self.native_usage_once)
            self.native_usage_once = None
        self.calls.append({"provider_id": provider_id, **kwargs})
        scene_id = kwargs["scene_id"]
        if self.fail_any_scene_once == scene_id:
            self.fail_any_scene_once = None
            raise RuntimeError(f"forced failure after {scene_id}")
        if scene_id == "event_index":
            if self.truncate_event_index_with_partial_once:
                self.truncate_event_index_with_partial_once = False
                raise ProviderAnalysisError(
                    "Provider output was truncated",
                    code="model_output_truncated",
                    partial_response='{"input_complete":true,"partial":"TRUNCATED_INDEX"}',
                )
            gap = self.index_gap_failures > 0
            self.index_gap_failures -= int(gap)
            payload = event_index_payload(gap=gap)
            if self.index_overlap_once:
                self.index_overlap_once = False
                duplicate = json.loads(json.dumps(payload["primary_sessions"][0]))
                duplicate["session_id"] = "communication-2"
                payload["primary_sessions"].append(duplicate)
            if self.invalid_index_schema_once:
                self.invalid_index_schema_once = False
                payload["unexpected"] = True
            return json.dumps(payload, ensure_ascii=False)
        if scene_id == "all_scenes_v1":
            if self.v1_error_code is not None:
                code = self.v1_error_code
                self.v1_error_code = None
                raise ProviderAnalysisError("V1 provider failure", code=code)
            if self.invalid_v1_json_once:
                self.invalid_v1_json_once = False
                return '{"input_complete": true,'
            payload = unified_v1_payload()
            if self.v1_privacy_leak == "candidate_id":
                payload["scene_results"][0]["cards"][0]["search_candidates"] = [{
                    "question": "查 communication-1", "purpose": "核验",
                    "related_segment_ids": ["seg_0_0"],
                }]
            elif self.v1_privacy_leak == "todo_reserved_name":
                payload["scene_results"][0]["todo_candidates"] = [{
                    "text": "请确认 work_communication_unit_ids", "owner_type": "user",
                    "assignee_text": None, "due_at": None, "due_text": None,
                    "evidence_segment_ids": ["seg_0_0"],
                }]
            elif self.v1_privacy_leak == "markdown_reserved_name":
                payload["scene_results"][0]["cards"][0]["markdown"] += (
                    "\n\nwork_communication_unit_ids"
                )
            if self.include_search_task:
                payload["scene_results"][0]["cards"][0]["search_candidates"] = [{
                    "question": "最新官方规格是什么？",
                    "purpose": "核验最新规格",
                    "related_segment_ids": ["seg_0_0"],
                }]
            if self.unsafe_v1_json_once is not None:
                invalid_kind = self.unsafe_v1_json_once
                self.unsafe_v1_json_once = None
                valid = json.dumps(payload, ensure_ascii=False)
                if invalid_kind == "brace_wrapped_garbage":
                    return '{"garbage" this is not JSON but it is wrapped in braces}'
                if invalid_kind == "front_damage":
                    return valid.replace('"input_complete":', '"input_complete"', 1)
                if invalid_kind == "multiple_damage":
                    return (
                        valid.replace('"input_complete":', '"input_complete"', 1)[:-1]
                        + ",}"
                    )
                raise AssertionError(f"unknown unsafe JSON fixture: {invalid_kind}")
            if self.missing_v1_scenes_once:
                self.missing_v1_scenes_once = False
                payload.pop("scene_results")
            if self.localizable_v1_json_once:
                self.localizable_v1_json_once = False
                return json.dumps(payload, ensure_ascii=False)[:-1] + ",}"
            if self.invalid_v1_schema_failures:
                self.invalid_v1_schema_failures -= 1
                payload["unexpected"] = True
            if self.incomplete_v1_once:
                self.incomplete_v1_once = False
                payload["input_complete"] = False
                payload["input_error"] = "模型未能完成全部场景"
            if self.unknown_v1_evidence_once:
                self.unknown_v1_evidence_once = False
                payload["scene_results"][0]["cards"][0]["source_segment_ids"] = [
                    "seg_9_9"
                ]
            if self.invalid_v1_markdown_once:
                self.invalid_v1_markdown_once = False
                payload["scene_results"][0]["cards"][0]["markdown"] = (
                    "# 结构不完整\n\n只有核心摘要，没有必要的正文章节。"
                )
            return json.dumps(payload, ensure_ascii=False)
        if scene_id in SCENE_IDS:
            self.started.append(scene_id)
            if self.block_scenes and len(self.started) == 7:
                self.release.set()
            if self.block_scenes:
                await asyncio.wait_for(self.release.wait(), timeout=2)
            if scene_id == self.fail_scene:
                self.fail_scene = None
                raise RuntimeError("scene failed")
            if scene_id == self.truncate_scene_once:
                self.truncate_scene_once = None
                raise ProviderAnalysisError(
                    "Provider output was truncated", code="model_output_truncated"
                )
            if scene_id == self.invalid_scene_once:
                self.invalid_scene_once = None
                return json.dumps({
                    "cards": [{
                        "markdown": "# 引用错误\n\n首次输出引用了不存在的片段。",
                        "source_segment_ids": ["seg_9_9"],
                        "search_candidates": [],
                    }],
                    "todo_candidates": [], "skip_reason": None,
                })
            if scene_id == self.misindexed_scene_once:
                self.misindexed_scene_once = None
                return json.dumps({
                    "cards": [{
                        "markdown": "# 编号错位\n\n证据片段序号真实，但文件序号有误。",
                        "source_segment_ids": ["seg_9_0"],
                        "search_candidates": [],
                    }],
                    "todo_candidates": [], "skip_reason": None,
                })
            if scene_id == "life_decisions":
                return json.dumps({"cards": [], "todo_candidates": [], "skip_reason": "无决策内容"})
            return json.dumps({
                "cards": [{
                    "markdown": f"# {scene_id}\n\n{scene_id} 的核心摘要。",
                    "source_segment_ids": ["seg_0_0"], "search_candidates": [],
                }],
                "todo_candidates": [], "skip_reason": None,
            })
        if scene_id == "unified_audit":
            if self.block_audits:
                self.audit_started += 1
                if self.audit_started % 2 == 0:
                    self.audit_release.set()
                await asyncio.wait_for(self.audit_release.wait(), timeout=2)
                if self.audit_started % 2 == 0:
                    self.audit_release = asyncio.Event()
            if self.audit_needs_large_budget and kwargs["max_tokens"] < 32_000:
                raise ProviderAnalysisError(
                    "Provider output was truncated", code="model_output_truncated"
                )
            unit_id = re.search(r'"audit_unit_id":"([^"]+)"', kwargs["user"]).group(1)
            phase = re.search(r'"audit_phase":"([^"]+)"', kwargs["user"]).group(1)
            scope = re.search(r'"audit_scope":"([^"]+)"', kwargs["user"]).group(1)
            if self.audit_unicode_privacy_leak or (
                self.audit_repair_privacy_leak and "上一次审核输出" in kwargs["system"]
            ):
                leaked = json.dumps({
                    "audit_phase": phase, "audit_scope": scope,
                    "audit_unit_id": unit_id, "input_complete": True,
                    "input_error": None, "issues": [{
                        "card_id": "card-01-01", "suggested_scene_id": None,
                        "issue_type": "unsupported_claim", "severity": "important",
                        "problem": "communication-1", "required_change": "删除",
                        "affected_excerpt": None, "evidence_segment_ids": [],
                    }], "revision_task_checks": [], "passed": False,
                })
                return leaked.replace("communication-1", r"\u0063ommunication-1")
            if self.invalid_audit_once:
                self.invalid_audit_once = False
                return json.dumps({
                    "audit_phase": phase, "audit_scope": scope,
                    "audit_unit_id": unit_id, "input_complete": True,
                    "input_error": None, "issues": [{
                        "card_id": "card-01-01", "suggested_scene_id": None,
                        "issue_type": "missed_high_value_content",
                        "severity": "important", "problem": "遗漏",
                        "required_change": "补充", "affected_excerpt": None,
                        "evidence_segment_ids": ["seg_0_0"],
                    }], "revision_task_checks": [], "passed": False,
                })
            issues = []
            requirement_ids = list(dict.fromkeys(re.findall(
                r'"requirement_id":"([^"]+)"', kwargs["user"]
            )))
            if (self.final_issue or self.final_issue_type) and phase == "final" and scope == "global_report":
                issues = [{
                    "card_id": "final-card-0001", "suggested_scene_id": None,
                    "issue_type": self.final_issue_type or "duplicate_content", "severity": "important",
                    "problem": "重复", "required_change": "删除重复",
                    "affected_excerpt": "重复", "evidence_segment_ids": [],
                }]
            return json.dumps({
                "audit_phase": phase, "audit_scope": scope, "audit_unit_id": unit_id,
                "input_complete": True, "input_error": None, "issues": issues,
                "revision_task_checks": [{
                    "requirement_id": requirement_id,
                    "completed": not (self.incomplete_revision and phase == "final"),
                    "reason": (
                        "未完成" if self.incomplete_revision and phase == "final" else "已完成"
                    ),
                    "evidence_segment_ids": ["seg_0_0"],
                } for requirement_id in requirement_ids],
                "passed": not issues and not (
                    self.incomplete_revision and phase == "final" and requirement_ids
                ),
            })
        if scene_id == "cross_card_orchestration":
            if self.invalid_orchestration_json_once:
                self.invalid_orchestration_json_once = False
                return '{"raw-marker":"never replay this"'
            if (
                self.orchestration_repair_privacy_leak
                and "上一次统筹输出" in kwargs["system"]
            ):
                return r'{"bad":"\u0063ommunication-1"}'
            if self.orchestration_needs_large_budget and kwargs["max_tokens"] < 32_000:
                raise ProviderAnalysisError(
                    "Provider output was truncated", code="model_output_truncated"
                )
            if self.invalid_orchestration_sequence:
                invalid_kind = self.invalid_orchestration_sequence.pop(0)
                if invalid_kind == "accepted_without_task":
                    bad_decision = {
                        "audit_issue_ids": ["issue-1"], "decision": "accepted",
                        "reason": "需要修改", "revision_task_key": None,
                    }
                    return json.dumps({
                        "input_complete": True, "input_error": None,
                        "audit_issue_decisions": [bad_decision],
                        "card_decisions": [], "revision_tasks": [],
                        "search_tasks": [], "dropped_search_candidates": [],
                        "todo_candidates": [], "dropped_todo_candidates": [],
                        "unresolved_conflicts": [],
                    })
                return json.dumps({
                    "input_complete": True, "input_error": None,
                    "audit_issue_decisions": [],
                    "card_decisions": [{
                        "decision_key": "decision-1", "operation": "create",
                        "source_card_ids": ["card-01-01"],
                        "target_scene_id": "work_communication", "reason": "新建",
                        "revision_task_key": "revision-1",
                    }],
                    "revision_tasks": [], "search_tasks": [],
                    "dropped_search_candidates": [], "todo_candidates": [],
                    "dropped_todo_candidates": [], "unresolved_conflicts": [],
                })
            if (
                self.orchestration_needs_convergence
                and "每个审核问题仍须恰好消费一次" not in kwargs["system"]
            ):
                raise ProviderAnalysisError(
                    "Provider output was truncated", code="model_output_truncated"
                )
            card_ids = list(dict.fromkeys(re.findall(r'"card_id":"([^"]+)"', kwargs["user"])))
            decisions = []
            tasks = []
            for index, card_id in enumerate(card_ids):
                operation = "revise" if self.revise and index == 0 else "keep"
                key = f"decision-{index + 1}"
                revision_key = "revision-1" if operation == "revise" else None
                decisions.append({
                    "decision_key": key, "operation": operation,
                    "source_card_ids": [card_id], "target_scene_id": SCENE_IDS[index],
                    "reason": "保留" if operation == "keep" else "修订",
                    "revision_task_key": revision_key,
                })
                if operation == "revise":
                    tasks.append({
                        "revision_task_key": revision_key, "decision_key": key,
                        "operation": "revise", "target_scene_id": SCENE_IDS[index],
                        "source_card_ids": [card_id], "audit_issue_ids": [],
                        "requirements": [{"requirement_id": "req-1", "instruction": "加强帮助", "evidence_segment_ids": ["seg_0_0"]}],
                        "preserve_points": [], "remove_or_avoid": [],
                        "completion_criteria": ["帮助明确"], "search_task_keys": [],
                    })
            if self.leak_internal_id_in_orchestration:
                decisions[0]["reason"] = "复制内部标识 communication-1"
            search_tasks = []
            if self.include_search_task:
                search_tasks = [{
                    "search_task_key": "search-task-1",
                    "source_candidate_ids": ["search-candidate-01-01-01"],
                    "question": "最新官方规格是什么？",
                    "purpose": "核验最新规格",
                    "target_decision_keys": ["decision-1"],
                    "related_segment_ids": ["seg_0_0"],
                    "source_requirements": ["官方来源"],
                    "jurisdiction": None,
                    "freshness_requirement": "当前版本",
                }]
            return json.dumps({
                "input_complete": True, "input_error": None, "audit_issue_decisions": [],
                "card_decisions": decisions, "revision_tasks": tasks,
                "search_tasks": search_tasks,
                "dropped_search_candidates": [], "todo_candidates": [],
                "dropped_todo_candidates": [], "unresolved_conflicts": [],
            })
        if scene_id == "targeted_revision":
            if self.invalid_revision_once:
                self.invalid_revision_once = False
                return json.dumps({
                    "input_complete": True, "input_error": None,
                    "reserved_card_id": "wrong-card-id", "operation": "create",
                    "markdown": "# 修订卡\n\n完成修订后的核心摘要。",
                    "source_segment_ids": ["seg_0_0"], "used_source_ids": [],
                    "todo_candidates": [], "completed_requirement_ids": ["req-1"],
                    "unresolved_requirement_ids": [],
                })
            if self.invalid_revision_markdown_once:
                self.invalid_revision_markdown_once = False
                return json.dumps({
                    "input_complete": True, "input_error": None,
                    "reserved_card_id": "final-card-0001", "operation": "revise",
                    "markdown": "# 修订卡\n\n## 正文\n缺少标题后的核心摘要。",
                    "source_segment_ids": ["seg_0_0"], "used_source_ids": [],
                    "todo_candidates": [], "completed_requirement_ids": ["req-1"],
                    "unresolved_requirement_ids": [],
                })
            return json.dumps({
                "input_complete": True, "input_error": None,
                "reserved_card_id": "final-card-0001", "operation": "revise",
                "markdown": "# 修订卡\n\n完成修订后的核心摘要。",
                "source_segment_ids": ["seg_0_0"], "used_source_ids": [],
                "todo_candidates": [], "completed_requirement_ids": ["req-1"],
                "unresolved_requirement_ids": [],
            })
        raise AssertionError(f"unexpected model call: {scene_id}")

    async def native_search(
        self, provider_id, *, queries, round_number, model_id, timeout_seconds=60
    ):
        if not self.include_search_task:
            raise AssertionError("no search expected")
        self.native_search_calls.append((provider_id, model_id, tuple(queries)))
        self.native_search_usage_totals.update({
            "input_tokens": 11,
            "output_tokens": 7,
            "response_count": 1,
            "tool_call_count": 1,
        })
        return NativeSearchCallResult(
            provider_id=provider_id,
            model_id=model_id,
            tool_name="$web_search",
            available=True,
            sources=(ExternalSource(
                source_id="provider-source-1",
                provider_id=provider_id,
                provider_result_id="result-1",
                title="官方规格",
                url="https://example.com/spec",
                publisher="Example",
                published_at="2026-09-04",
                support_statement="官方规格已更新。",
                search_round=1,
            ),),
        )


async def seed(
    database: Database,
    *,
    pipeline_kind: str = "beta8_indexed_scene_v2",
    report_model_id: str = "deepseek-v4-pro",
    search_provider_id: str | None = None,
    search_model_id: str | None = None,
) -> None:
    _, parameters_json, parameters_fingerprint = build_pipeline_parameters(
        pipeline_kind=pipeline_kind,
        provider_id="deepseek",
        model_id=report_model_id,
        credential_generation=1,
        search_provider_id=search_provider_id,
        search_model_id=search_model_id,
    )
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="analyzing"))
        session.add(JobFile(
            id="file-1", job_id="job-1", original_name="全天.mp3", extension=".mp3",
            size_bytes=10, sha256="a" * 64, duration_ms=10_000,
            recording_started_at=None, recording_time_source="unknown",
            timezone="Asia/Shanghai", position=0, temporary_path="/tmp/fixture.mp3",
        ))
        session.add(Transcript(
            id="transcript-1", job_file_id="file-1", segment_index=0,
            speaker_id="speaker_1", start_ms=0, end_ms=1_000,
            text="我最近主要关注 AI 硬件。", words_json="[]",
            risk_classified=True, is_reliable=True,
        ))
        session.add(AnalysisVersion(
            id="version-1", source_job_id="job-1", provider_id="deepseek",
            model_id=report_model_id, credential_generation=1,
            prompt_snapshot_json="{}", profile_snapshot_json="[]",
            fixed_rules_hash=Beta8PromptComposer.fixed_rules_hash(),
            staged_results_json="{}", pipeline_parameters_json=parameters_json,
            pipeline_parameters_fingerprint=parameters_fingerprint,
            status="running", worker_owner_id="worker-1",
        ))
        await session.commit()


async def harness(
    tmp_path, *, pipeline_kind="beta8_indexed_scene_v2",
    report_model_id="deepseek-v4-pro",
    stop_after_event_index_checkpoint=False,
    stop_after_v1_checkpoint=False,
    response_quarantine=None,
    event_index_gate=None,
    runner_class=Beta8ReportRunner, search_executor=None,
    search_provider_id=None, search_model_id=None,
    startup_search_provider_id=None, startup_search_model_id=None,
    **provider_kwargs
):
    database = Database(tmp_path / "beta8.sqlite3")
    await database.create_schema()
    await seed(
        database,
        pipeline_kind=pipeline_kind,
        report_model_id=report_model_id,
        search_provider_id=search_provider_id,
        search_model_id=search_model_id,
    )
    provider = Provider(**provider_kwargs)
    publisher = Publisher()
    runner = runner_class(
        database=database, provider=provider, publisher=publisher,
        generation_source=GenerationSource(),
        stop_after_event_index_checkpoint=stop_after_event_index_checkpoint,
        stop_after_v1_checkpoint=stop_after_v1_checkpoint,
        response_quarantine=response_quarantine,
        event_index_gate=event_index_gate,
        search_executor=(
            search_executor
            or Beta8SearchExecutor(
                provider,
                provider_id=startup_search_provider_id,
                model_id=startup_search_model_id,
            )
        ),
    )
    return database, provider, publisher, runner


@pytest.mark.asyncio
async def test_event_index_gate_stops_before_unified_v1_call(tmp_path) -> None:
    def reject_boundary(_index) -> None:
        raise ValueError("communication boundary mismatch")

    database, provider, publisher, runner = await harness(
        tmp_path,
        event_index_gate=reject_boundary,
    )

    with pytest.raises(ValueError, match="communication boundary mismatch"):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_event_index_raw_is_quarantined_before_boundary_gate(tmp_path) -> None:
    captured: list[tuple[str, str]] = []

    def reject_boundary(_index) -> None:
        raise ValueError("communication boundary mismatch")

    database, provider, publisher, runner = await harness(
        tmp_path,
        event_index_gate=reject_boundary,
        response_quarantine=lambda stage, raw: captured.append((stage, raw)),
    )

    with pytest.raises(ValueError, match="communication boundary mismatch"):
        await runner.run("version-1", "worker-1")

    assert len(captured) == 1
    assert captured[0][0] == "event_index"
    assert json.loads(captured[0][1])["primary_sessions"][0]["session_id"] == "communication-1"
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_quarantines_then_normalizes_before_gate_and_checkpoint(
    tmp_path, monkeypatch,
) -> None:
    events: list[str] = []
    original_normalize = beta8_runner_module.normalize_event_index_draft

    def record_normalize(draft, transcript):
        events.append("normalize")
        return original_normalize(draft, transcript)

    class RecordingCheckpointRunner(Beta8ReportRunner):
        async def _save_stage(self, *args, **kwargs):
            events.append("checkpoint")
            return await super()._save_stage(*args, **kwargs)

    monkeypatch.setattr(
        beta8_runner_module, "normalize_event_index_draft", record_normalize
    )
    database, provider, publisher, runner = await harness(
        tmp_path,
        stop_after_event_index_checkpoint=True,
        runner_class=RecordingCheckpointRunner,
        response_quarantine=lambda _stage, _raw: events.append("quarantine"),
        event_index_gate=lambda _index: events.append("gate"),
    )

    with pytest.raises(Beta8CheckpointPause):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert events == ["quarantine", "normalize", "gate", "checkpoint"]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_truncated_event_index_partial_response_is_quarantined(tmp_path) -> None:
    captured: list[tuple[str, str]] = []
    database, provider, publisher, runner = await harness(
        tmp_path,
        truncate_event_index_with_partial_once=True,
        response_quarantine=lambda stage, raw: captured.append((stage, raw)),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1", "worker-1")

    assert raised.value.code == "model_output_truncated"
    assert captured == [
        (
            "event_index_truncated",
            '{"input_complete":true,"partial":"TRUNCATED_INDEX"}',
        )
    ]
    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert publisher.bundles == []
    await database.dispose()


class BindingRecordingSearchExecutor:
    def __init__(
        self,
        *,
        provider_id: str | None = "current-search-provider",
        model_id: str | None = "current-search-model",
        bindings: list[tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.bindings = bindings if bindings is not None else []

    def with_binding(self, provider_id, model_id):
        self.bindings.append((provider_id, model_id))
        return BindingRecordingSearchExecutor(
            provider_id=provider_id,
            model_id=model_id,
            bindings=self.bindings,
        )

    async def execute(self, tasks, *, completed, persist):
        return tuple(completed.values())


async def load_staged(database: Database) -> dict[str, object]:
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    return json.loads(version.staged_results_json)


async def save_staged(database: Database, staged: dict[str, object]) -> None:
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        version.staged_results_json = json.dumps(staged, ensure_ascii=False)
        await session.commit()


async def lose_version_lease(database: Database, mutation: str) -> None:
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        if mutation == "owner":
            version.worker_owner_id = "worker-2"
        elif mutation == "status":
            version.status = "failed"
        else:
            raise AssertionError(f"unknown lease mutation: {mutation}")
        await session.commit()


class LeaseBoundaryRunner(Beta8ReportRunner):
    lose_before_stage: tuple[str, str] | None = None
    lose_after_stage: tuple[str, str] | None = None
    lose_after_generate: tuple[str, str] | None = None
    lose_before_metrics: str | None = None

    async def _generate(self, version, request, **kwargs):
        result = await super()._generate(version, request, **kwargs)
        if (
            self.lose_after_generate is not None
            and request.scene_id == self.lose_after_generate[0]
        ):
            _, mutation = self.lose_after_generate
            self.lose_after_generate = None
            await lose_version_lease(self.database, mutation)
        return result

    async def _save_stage(self, *args, **kwargs):
        key = args[2]
        if self.lose_before_stage is not None and key == self.lose_before_stage[0]:
            _, mutation = self.lose_before_stage
            self.lose_before_stage = None
            await lose_version_lease(self.database, mutation)
        result = await super()._save_stage(*args, **kwargs)
        if self.lose_after_stage is not None and key == self.lose_after_stage[0]:
            _, mutation = self.lose_after_stage
            self.lose_after_stage = None
            await lose_version_lease(self.database, mutation)
        return result

    async def _save_metrics(self, *args, **kwargs):
        if self.lose_before_metrics is not None:
            mutation = self.lose_before_metrics
            self.lose_before_metrics = None
            await lose_version_lease(self.database, mutation)
        return await super()._save_metrics(*args, **kwargs)


class FailureBoundaryRunner(Beta8ReportRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_search = False
        self.fail_final_requirements = False
        self.metric_saves = 0

    async def _run_search(self, *args, **kwargs):
        packets = await super()._run_search(*args, **kwargs)
        if self.fail_search:
            raise RuntimeError("forced failure after search")
        return packets

    def _validate_final_requirement_checks(self, staged, requirements):
        if self.fail_final_requirements:
            raise RuntimeError("forced failure at final requirement validation")
        return super()._validate_final_requirement_checks(staged, requirements)

    async def _save_metrics(self, *args, **kwargs):
        self.metric_saves += 1
        return await super()._save_metrics(*args, **kwargs)


def test_checkpoint_keys_are_stable_and_complete() -> None:
    assert BETA8_STAGE_KEYS == (
        "beta8_event_index", "beta8_all_scenes_v1",
        "beta8_initial_audit_units", "beta8_initial_audit_aggregate",
        "beta8_orchestration", "beta8_search_packets", "beta8_revised_cards",
        "beta8_final_candidate", "beta8_final_audit_units",
        "beta8_final_audit_aggregate", "beta8_publication_ready",
    )


def search_packet(*, task_id: str, title: str = "Official", publisher: str | None = "Example") -> Beta8SearchPacket:
    return Beta8SearchPacket.model_validate({
        "search_task_id": task_id, "status": "success", "answer": "支持",
        "supported_points": [{"point": "支持", "source_ids": [f"provider-{task_id}"]}],
        "unresolved_points": [], "error_summary": None,
        "sources": [{
            "source_id": f"provider-{task_id}", "title": title,
            "url": "HTTPS://Example.com:443/doc?utm_source=task",
            "publisher": publisher, "published_at": None,
            "retrieved_at": f"2026-09-02T12:00:0{1 if task_id == 'one' else 2}+08:00",
            "source_type": "provider_native_search", "supports": [0],
        }],
    })


def test_runner_registry_dedupes_same_canonical_source_from_two_real_task_packets() -> None:
    registry = build_strict_source_registry((
        search_packet(task_id="one"), search_packet(task_id="two"),
    ))

    assert len(registry) == 1
    assert registry[0].url == "https://example.com/doc"
    assert registry[0].supports == []


def test_runner_registry_fails_closed_on_conflicting_metadata_in_two_task_packets() -> None:
    with pytest.raises(ValueError, match="conflicting source registry metadata"):
        build_strict_source_registry((
            search_packet(task_id="one", title="Official"),
            search_packet(task_id="two", title="Different official title"),
        ))


@pytest.mark.asyncio
async def test_percent_decoded_restored_search_leak_writes_zero_checkpoint(tmp_path) -> None:
    database, _, _, runner = await harness(tmp_path)
    packet = search_packet(task_id="one").model_dump(mode="json")
    packet["sources"][0]["url"] = "https://example.com/communication%2D1"
    record = Beta8StageRecord.create(
        payload={"one": packet},
        prompt_hash=runner.composer.fixed_rules_hash(),
        transcript_fingerprint="b" * 64,
        provider_generation=0,
        upstream_artifact_hash="c" * 64,
    )
    staged = {"beta8_search_packets": record.model_dump(mode="json")}

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner._run_search(
            SimpleNamespace(id="version-1"),
            staged,
            SimpleNamespace(search_tasks=[]),
            "b" * 64,
            "c" * 64,
            "worker-1",
            BindingRecordingSearchExecutor(provider_id=None),
            {"card-01-01": ("communication-1",)},
        )

    persisted = await load_staged(database)
    assert "beta8_search_packets" not in persisted
    await database.dispose()


def test_checkpoint_rejects_changed_compatibility_inputs() -> None:
    record = Beta8StageRecord.create(
        payload={"ok": True}, prompt_hash="a" * 64, transcript_fingerprint="b" * 64,
        provider_generation=1, upstream_artifact_hash="c" * 64,
    )
    for field, value in [
        ("prompt_hash", "d" * 64), ("transcript_fingerprint", "d" * 64),
        ("provider_generation", 2), ("upstream_artifact_hash", "d" * 64),
        ("schema_version", 2),
    ]:
        kwargs = dict(prompt_hash="a" * 64, transcript_fingerprint="b" * 64,
                      provider_generation=1, upstream_artifact_hash="c" * 64,
                      schema_version=1)
        kwargs[field] = value
        with pytest.raises(ValueError, match=field):
            validate_stage_record(record, **kwargs)


@pytest.mark.asyncio
async def test_legacy_beta8_pipeline_keeps_seven_scene_v1_calls(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, pipeline_kind="beta8_multi_scene_v1"
    )

    await runner.run("version-1", "worker-1")

    assert provider.started == list(SCENE_IDS)
    assert not {"event_index", "all_scenes_v1"}.intersection(
        call["scene_id"] for call in provider.calls
    )
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_calls_event_index_then_complete_v1_exactly_once_each(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(tmp_path)

    await runner.run("version-1", "worker-1")

    stage_ids = [call["scene_id"] for call in provider.calls]
    assert [
        stage_id for stage_id in stage_ids
        if stage_id in {"event_index", "all_scenes_v1"}
    ] == [
        "event_index",
        "all_scenes_v1",
    ]
    assert stage_ids.count("event_index") == 1
    assert stage_ids.count("all_scenes_v1") == 1
    assert provider.calls[0]["thinking_enabled"] is True
    assert provider.calls[0]["max_tokens"] == 32_000
    index_transcript = request_payload(provider.calls[0])["transcript_markdown"]
    v1_transcript = request_payload(provider.calls[1])["transcript_markdown"]
    assert index_transcript.encode() == v1_transcript.encode()
    assert provider.calls[1]["thinking_enabled"] is True
    assert provider.calls[1]["max_tokens"] == 64_000
    assert provider.calls[1]["timeout_seconds"] == 600.0
    staged = await load_staged(database)
    index_record = Beta8StageRecord.model_validate(staged["beta8_event_index"])
    v1_record = Beta8StageRecord.model_validate(staged["beta8_all_scenes_v1"])
    assert {
        "primary_sessions",
        "embedded_events",
        "coverage_ranges",
        "system_excluded_ranges",
    } <= set(index_record.payload)
    assert "file_timelines" not in index_record.payload
    assert "activity_sessions" not in index_record.payload
    assert index_record.upstream_artifact_hash == sha256(b"").hexdigest()
    assert v1_record.upstream_artifact_hash == canonical_hash(index_record.payload)
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_flash_smoke_gives_event_index_a_larger_completion_budget(
    tmp_path,
) -> None:
    database, provider, _publisher, runner = await harness(
        tmp_path,
        report_model_id="deepseek-v4-flash",
    )

    await runner.run("version-1", "worker-1")

    assert provider.calls[0]["scene_id"] == "event_index"
    assert provider.calls[0]["thinking_enabled"] is True
    assert provider.calls[0]["max_tokens"] == 64_000
    await database.dispose()


@pytest.mark.asyncio
async def test_paid_stage_pause_saves_v1_before_any_audit_or_search_call(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path,
        stop_after_v1_checkpoint=True,
    )

    with pytest.raises(Beta8CheckpointPause, match="all_scenes_v1"):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == [
        "event_index",
        "all_scenes_v1",
    ]
    staged = await load_staged(database)
    assert {"beta8_event_index", "beta8_all_scenes_v1"}.issubset(staged)
    assert publisher.bundles == []
    assert provider.native_search_calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_paid_v1_pause_never_spends_a_second_call_on_structure_repair(
    tmp_path,
) -> None:
    """A V1-only cost cap must preserve the raw failure without paid repair."""
    database, provider, publisher, runner = await harness(
        tmp_path,
        stop_after_v1_checkpoint=True,
        invalid_v1_schema_failures=1,
    )

    with pytest.raises(ProviderAnalysisError, match="structurally incomplete"):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == [
        "event_index",
        "all_scenes_v1",
    ]
    assert set(await load_staged(database)) == {"beta8_event_index"}
    assert publisher.bundles == []
    assert provider.native_search_calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_paid_index_pause_saves_index_before_v1_call(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path,
        stop_after_event_index_checkpoint=True,
    )

    with pytest.raises(Beta8CheckpointPause) as caught:
        await runner.run("version-1", "worker-1")

    assert caught.value.checkpoint_stage == "event_index"
    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert set(await load_staged(database)) == {"beta8_event_index"}
    assert publisher.bundles == []
    assert provider.native_search_calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_cached_canonical_index_reruns_injected_gate_without_provider_call(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, stop_after_event_index_checkpoint=True
    )
    with pytest.raises(Beta8CheckpointPause):
        await runner.run("version-1", "worker-1")
    provider.calls.clear()
    gated: list[str] = []
    resumed = Beta8ReportRunner(
        database=database,
        provider=provider,
        publisher=publisher,
        generation_source=GenerationSource(),
        search_executor=Beta8SearchExecutor(
            provider, provider_id=None, model_id=None
        ),
        stop_after_event_index_checkpoint=True,
        event_index_gate=lambda _index: gated.append("gate"),
    )

    with pytest.raises(Beta8CheckpointPause):
        await resumed.run("version-1", "worker-1")

    assert gated == ["gate"]
    assert provider.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_paid_index_pause_never_spends_a_second_call_on_coverage_repair(
    tmp_path,
) -> None:
    """Break caught: an index-only cost cap must not silently pay for repair."""
    captured: list[tuple[str, str]] = []
    database, provider, publisher, runner = await harness(
        tmp_path,
        stop_after_event_index_checkpoint=True,
        index_gap_failures=1,
        response_quarantine=lambda stage, raw: captured.append((stage, raw)),
    )

    with pytest.raises(EventIndexCoverageError, match="missing file timeline"):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert [stage for stage, _raw in captured] == ["event_index"]
    assert await load_staged(database) == {}
    assert publisher.bundles == []
    assert provider.native_search_calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_uses_frozen_version_search_binding_not_current_config(
    tmp_path,
) -> None:
    search = BindingRecordingSearchExecutor()
    database, provider, publisher, runner = await harness(
        tmp_path,
        search_executor=search,
        search_provider_id="frozen-search-provider",
        search_model_id="frozen-search-model",
    )

    await runner.run("version-1", "worker-1")

    assert search.bindings == [
        ("frozen-search-provider", "frozen-search-model")
    ]
    restarted_search = BindingRecordingSearchExecutor(
        provider_id="different-current-provider",
        model_id="different-current-model",
    )
    restarted_runner = Beta8ReportRunner(
        database=database,
        provider=provider,
        publisher=publisher,
        generation_source=GenerationSource(),
        search_executor=restarted_search,
    )

    await restarted_runner.run("version-1", "worker-1")

    assert restarted_search.bindings == [
        ("frozen-search-provider", "frozen-search-model")
    ]
    assert len(publisher.bundles) == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_metrics_use_frozen_search_when_startup_has_no_search_provider(
    tmp_path,
) -> None:
    database, provider, _, runner = await harness(
        tmp_path,
        search_provider_id="frozen-search-provider",
        search_model_id="frozen-search-model",
        startup_search_provider_id=None,
        startup_search_model_id=None,
        include_search_task=True,
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert provider.native_search_calls == [(
        "frozen-search-provider",
        "frozen-search-model",
        ("最新官方规格是什么？",),
    )]
    assert metrics["web_search_performed"] is True
    assert metrics["web_search_degraded_reason"] is None
    assert metrics["search_model_response_count"] == 1
    assert metrics["web_search_tool_call_count"] == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_metrics_ignore_startup_search_when_frozen_version_has_none(
    tmp_path,
) -> None:
    database, provider, _, runner = await harness(
        tmp_path,
        search_provider_id=None,
        search_model_id=None,
        startup_search_provider_id="startup-search-provider",
        startup_search_model_id="startup-search-model",
        include_search_task=True,
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert provider.native_search_calls == []
    assert metrics["web_search_performed"] is False
    assert "not configured" in metrics["web_search_degraded_reason"]
    assert metrics["search_model_response_count"] == 0
    assert metrics["web_search_tool_call_count"] == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_index_normalization_failure_never_implicitly_buys_a_repair(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, index_gap_failures=1
    )

    with pytest.raises(EventIndexCoverageError, match="missing file timeline"):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert await load_staged(database) == {}
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_repeated_index_failure_configuration_still_makes_only_one_call(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, index_gap_failures=2
    )

    with pytest.raises(EventIndexCoverageError, match="missing file timeline"):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_kwargs", "expected_error"),
    [
        ({"index_overlap_once": True}, EventIndexCoverageError),
        ({"invalid_index_schema_once": True}, ProviderAnalysisError),
    ],
)
async def test_non_gap_index_errors_fail_without_repair(
    tmp_path, provider_kwargs, expected_error
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, **provider_kwargs
    )

    with pytest.raises(expected_error):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert provider.calls[0]["repair_attempted"] is False
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_from_index_checkpoint_only_calls_v1(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, v1_error_code="network_timeout"
    )
    with pytest.raises(ProviderAnalysisError, match="V1 provider failure"):
        await runner.run("version-1", "worker-1")
    provider.calls.clear()

    await runner.run("version-1", "worker-1")

    assert provider.calls[0]["scene_id"] == "all_scenes_v1"
    assert "event_index" not in [call["scene_id"] for call in provider.calls]
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_from_v1_checkpoint_calls_neither_v1_stage(tmp_path) -> None:
    database, provider, publisher, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    provider.calls.clear()

    await runner.run("version-1", "worker-1")

    v1_call_stages = {"event_index", "all_scenes_v1"}
    assert not v1_call_stages.intersection(
        call["scene_id"] for call in provider.calls
    )
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    staged = json.loads(version.staged_results_json)
    assert {"beta8_event_index", "beta8_all_scenes_v1"}.issubset(staged)
    assert len(publisher.bundles) == 2
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["prompt_hash", "schema_version"])
async def test_changed_prompt_or_schema_invalidates_index_checkpoint(
    tmp_path, changed_field
) -> None:
    database, provider, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    staged = await load_staged(database)
    record = Beta8StageRecord.model_validate(staged["beta8_event_index"])
    stale_value = "0" * 64 if changed_field == "prompt_hash" else 2
    staged["beta8_event_index"] = record.model_copy(
        update={changed_field: stale_value}
    ).model_dump(mode="json")
    await save_staged(database, staged)
    provider.calls.clear()

    await runner.run("version-1", "worker-1")

    assert provider.calls[0]["scene_id"] == "event_index"
    await database.dispose()


@pytest.mark.asyncio
async def test_changed_transcript_invalidates_index_and_v1_checkpoints(tmp_path) -> None:
    database, provider, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    async with database.session() as session:
        transcript = await session.get(Transcript, "transcript-1")
        transcript.text = "我现在转而关注空间计算。"
        await session.commit()
    provider.calls.clear()

    await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls[:2]] == [
        "event_index",
        "all_scenes_v1",
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_changed_index_hash_invalidates_only_v1_checkpoint(tmp_path) -> None:
    database, provider, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    staged = await load_staged(database)
    record = Beta8StageRecord.model_validate(staged["beta8_event_index"])
    payload = json.loads(json.dumps(record.payload, ensure_ascii=False))
    payload["primary_sessions"][0]["description"] = "同一范围的更新索引描述"
    staged["beta8_event_index"] = Beta8StageRecord.create(
        payload=payload,
        prompt_hash=record.prompt_hash,
        transcript_fingerprint=record.transcript_fingerprint,
        provider_generation=record.provider_generation,
        upstream_artifact_hash=sha256(b"").hexdigest(),
    ).model_dump(mode="json")
    await save_staged(database, staged)
    provider.calls.clear()

    await runner.run("version-1", "worker-1")

    assert provider.calls[0]["scene_id"] == "all_scenes_v1"
    assert "event_index" not in [call["scene_id"] for call in provider.calls]
    await database.dispose()


@pytest.mark.asyncio
async def test_corrupt_checkpoint_payload_still_fails_closed(tmp_path) -> None:
    database, provider, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    staged = await load_staged(database)
    staged["beta8_event_index"]["payload"]["primary_sessions"][0]["description"] = "被篡改"
    await save_staged(database, staged)
    provider.calls.clear()

    with pytest.raises(ValueError, match="artifact_hash"):
        await runner.run("version-1", "worker-1")

    assert provider.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_corrupt_checkpoint_fails_closed_before_all_compatibility_misses(
    tmp_path,
) -> None:
    database, provider, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    staged = await load_staged(database)
    record = Beta8StageRecord.model_validate(staged["beta8_event_index"])
    payload = json.loads(json.dumps(record.payload, ensure_ascii=False))
    payload["primary_sessions"][0]["description"] = "payload corruption"
    staged["beta8_event_index"] = record.model_copy(update={
        "payload": payload,
        "prompt_hash": "0" * 64,
        "schema_version": record.schema_version + 1,
        "transcript_fingerprint": "1" * 64,
        "provider_generation": record.provider_generation + 1,
        "upstream_artifact_hash": "2" * 64,
    }).model_dump(mode="json")
    await save_staged(database, staged)
    provider.calls.clear()

    with pytest.raises(ValueError, match="artifact_hash.*corrupt"):
        await runner.run("version-1", "worker-1")

    assert provider.calls == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["model_output_truncated", "network_timeout"])
async def test_v1_provider_failure_never_falls_back_to_seven_scene_calls(
    tmp_path, error_code
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, v1_error_code=error_code
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1", "worker-1")

    assert raised.value.code == error_code
    assert all(call["scene_id"] not in SCENE_IDS for call in provider.calls)
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [("owner", LeaseLostError), ("status", ValueError)],
)
async def test_lease_loss_before_index_checkpoint_save_writes_no_checkpoint(
    tmp_path, mutation, expected_error
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, runner_class=LeaseBoundaryRunner
    )
    runner.lose_before_stage = ("beta8_event_index", mutation)

    with pytest.raises(expected_error):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert await load_staged(database) == {}
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [("owner", LeaseLostError), ("status", ValueError)],
)
async def test_lease_loss_after_index_checkpoint_stops_before_v1(
    tmp_path, mutation, expected_error
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, runner_class=LeaseBoundaryRunner
    )
    runner.lose_after_stage = ("beta8_event_index", mutation)

    with pytest.raises(expected_error):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == ["event_index"]
    assert set(await load_staged(database)) == {"beta8_event_index"}
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [("owner", LeaseLostError), ("status", ValueError)],
)
async def test_lease_loss_after_v1_return_rejects_v1_checkpoint(
    tmp_path, mutation, expected_error
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, runner_class=LeaseBoundaryRunner
    )
    runner.lose_after_generate = ("all_scenes_v1", mutation)

    with pytest.raises(expected_error):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == [
        "event_index", "all_scenes_v1"
    ]
    assert set(await load_staged(database)) == {"beta8_event_index"}
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("owner", LeaseLostError),
        ("status", ProviderAnalysisError),
    ],
)
async def test_failed_v1_metrics_save_surfaces_lease_loss(
    tmp_path, mutation, expected_error
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, runner_class=LeaseBoundaryRunner,
        v1_error_code="network_timeout",
    )
    runner.lose_before_metrics = mutation

    with pytest.raises(expected_error):
        await runner.run("version-1", "worker-1")

    assert [call["scene_id"] for call in provider.calls] == [
        "event_index", "all_scenes_v1"
    ]
    assert set(await load_staged(database)) == {"beta8_event_index"}
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert json.loads(version.pipeline_metrics_json)["model_call_count"] == 1
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_v1_schema_error_gets_one_structure_only_repair(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_v1_schema_failures=1
    )

    await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls if call["scene_id"] == "all_scenes_v1"
    ]
    assert len(calls) == 2
    assert calls[1]["repair_attempted"] is True
    assert "unexpected" in calls[1]["system"]
    assert "beta8_invalid_all_scenes_v1_output" in calls[1]["user"]
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_v1_structure_repair_is_bounded_to_one_attempt(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_v1_schema_failures=2
    )

    with pytest.raises(ProviderAnalysisError, match="structure repair failed"):
        await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls if call["scene_id"] == "all_scenes_v1"
    ]
    assert len(calls) == 2
    assert [call["repair_attempted"] for call in calls] == [False, True]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_localizable_complete_json_error_gets_one_structure_repair(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, localizable_v1_json_once=True
    )

    await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls if call["scene_id"] == "all_scenes_v1"
    ]
    assert len(calls) == 2
    assert calls[1]["repair_attempted"] is True
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_kind",
    ["brace_wrapped_garbage", "front_damage", "multiple_damage"],
)
async def test_unsafe_json_damage_never_enters_structure_repair(
    tmp_path, invalid_kind
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, unsafe_v1_json_once=invalid_kind
    )

    with pytest.raises(ProviderAnalysisError, match="incomplete JSON"):
        await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls if call["scene_id"] == "all_scenes_v1"
    ]
    assert len(calls) == 1
    assert calls[0]["repair_attempted"] is False
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_truncated_json_and_incomplete_v1_fail_without_structure_repair(
    tmp_path,
) -> None:
    for case in ("json", "incomplete", "missing_scenes"):
        case_path = tmp_path / case
        kwargs = {
            "json": {"invalid_v1_json_once": True},
            "incomplete": {"incomplete_v1_once": True},
            "missing_scenes": {"missing_v1_scenes_once": True},
        }[case]
        database, provider, publisher, runner = await harness(case_path, **kwargs)

        with pytest.raises(ProviderAnalysisError):
            await runner.run("version-1", "worker-1")

        calls = [
            call for call in provider.calls if call["scene_id"] == "all_scenes_v1"
        ]
        assert len(calls) == 1
        assert publisher.bundles == []
        await database.dispose()


@pytest.mark.asyncio
async def test_zero_card_scene_is_valid(tmp_path) -> None:
    database, _, publisher, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    assert len(publisher.bundles[0].cards) == 6
    await database.dispose()


@pytest.mark.asyncio
async def test_unified_scene_outputs_keep_existing_downstream_shapes(tmp_path) -> None:
    database, _, _, runner = await harness(tmp_path)
    payload = unified_v1_payload()
    work_card = payload["scene_results"][0]["cards"][0]
    work_card["search_candidates"] = [{
        "question": "需要查什么？",
        "purpose": "帮助判断",
        "related_segment_ids": ["seg_0_0"],
    }]
    payload["scene_results"][0]["todo_candidates"] = [{
        "text": "确认后续方案",
        "owner_type": "user",
        "assignee_text": None,
        "due_at": None,
        "due_text": None,
        "evidence_segment_ids": ["seg_0_0"],
    }]
    unified = Beta8UnifiedV1Result.model_validate(payload)

    cards, searches, todos = runner._materialize_scene_outputs(unified)

    assert cards[0]["card_id"] == "card-01-01"
    assert cards[0]["scene_id"] == "work_communication"
    assert "work_communication_unit_ids" not in cards[0]
    assert runner._work_unit_ids_by_card_id(unified)["card-01-01"] == (
        "communication-1",
    )
    assert cards[0]["v1_markdown_sha256"] == sha256(
        cards[0]["markdown"].encode()
    ).hexdigest()
    assert searches == [{
        "search_candidate_id": "search-candidate-01-01-01",
        "card_id": "card-01-01",
        "question": "需要查什么？",
        "purpose": "帮助判断",
        "related_segment_ids": ["seg_0_0"],
    }]
    assert todos == [{
        "todo_candidate_id": "todo-candidate-01-01",
        "scene_id": "work_communication",
        "text": "确认后续方案",
        "owner_type": "user",
        "assignee_text": None,
        "due_at": None,
        "due_text": None,
        "evidence_segment_ids": ["seg_0_0"],
    }]
    await database.dispose()


@pytest.mark.asyncio
async def test_revise_preserves_work_communication_mapping_through_publication(tmp_path) -> None:
    database, _, publisher, runner = await harness(tmp_path, revise=True)

    await runner.run("version-1", "worker-1")

    bundle = publisher.bundles[0]
    assert bundle.expected_work_communication_unit_ids == ["communication-1"]
    assert bundle.cards[0].scene_id == "work_communication"
    assert bundle.cards[0].work_communication_unit_ids == ["communication-1"]
    assert "communication-1" not in bundle.cards[0].markdown
    await database.dispose()


@pytest.mark.asyncio
async def test_unknown_v1_evidence_fails_without_schema_repair(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, unknown_v1_evidence_once=True
    )

    with pytest.raises(ValueError, match="seg_9_9"):
        await runner.run("version-1", "worker-1")

    v1_calls = [
        call for call in provider.calls if call["scene_id"] == "all_scenes_v1"
    ]
    assert len(v1_calls) == 1
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_v1_markdown_quality_failure_is_not_disguised_as_schema_repair(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_v1_markdown_once=True
    )

    with pytest.raises(ValueError, match="work_communication card 1"):
        await runner.run("version-1", "worker-1")

    v1_calls = [
        call for call in provider.calls if call["scene_id"] == "all_scenes_v1"
    ]
    assert len(v1_calls) == 1
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_audit_units_have_enough_output_budget_for_complete_issue_lists(tmp_path) -> None:
    database, _, publisher, runner = await harness(
        tmp_path, audit_needs_large_budget=True
    )

    await runner.run("version-1", "worker-1")

    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_invalid_audit_contract_gets_one_targeted_repair(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_audit_once=True
    )

    await runner.run("version-1", "worker-1")

    audit_calls = [
        call for call in provider.calls if call["scene_id"] == "unified_audit"
    ]
    repair_calls = [call for call in audit_calls if call["repair_attempted"]]
    assert len(repair_calls) == 1
    assert "missed_high_value_content requires suggested_scene_id" in repair_calls[0][
        "system"
    ]
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_orchestration_has_enough_output_budget_for_all_decisions(tmp_path) -> None:
    database, _, publisher, runner = await harness(
        tmp_path, orchestration_needs_large_budget=True
    )

    await runner.run("version-1", "worker-1")

    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_orchestration_converges_without_dropping_audit_issue_coverage(tmp_path) -> None:
    database, _, publisher, runner = await harness(
        tmp_path, orchestration_needs_convergence=True
    )

    await runner.run("version-1", "worker-1")

    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_invalid_orchestration_contract_gets_one_targeted_repair(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_orchestration_once=True
    )

    await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls
        if call["scene_id"] == "cross_card_orchestration"
    ]
    assert len(calls) == 2
    assert calls[1]["repair_attempted"] is True
    assert "accepted or combined decision requires a revision task" in calls[1][
        "system"
    ]
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_orchestration_repair_uses_previous_output_and_is_bounded(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_orchestration_twice=True
    )

    await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls
        if call["scene_id"] == "cross_card_orchestration"
    ]
    assert len(calls) == 3
    assert calls[1]["repair_attempted"] is True
    assert calls[2]["repair_attempted"] is True
    assert "create cannot have source cards" in calls[2]["system"]
    assert "beta8_invalid_orchestration_output" in calls[2]["user"]
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_unparseable_orchestration_output_is_not_replayed_to_repair(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_orchestration_json_once=True
    )

    await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls
        if call["scene_id"] == "cross_card_orchestration"
    ]
    assert len(calls) == 2
    assert calls[1]["repair_attempted"] is True
    assert "raw-marker" not in calls[1]["user"]
    assert "beta8_invalid_orchestration_output" not in calls[1]["user"]
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_revision_identity_mismatch_gets_targeted_repair(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, revise=True, invalid_revision_once=True
    )

    await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls if call["scene_id"] == "targeted_revision"
    ]
    assert len(calls) == 2
    assert calls[1]["repair_attempted"] is True
    assert "final-card-0001" in calls[1]["system"]
    assert "operation=revise" in calls[1]["system"]
    expected_user_keys = {
        "source_cards", "revision_task", "transcript_segments", "search_packets",
    }
    for call in calls:
        assert call["user"].startswith(
            '<beta8_revision_data untrusted="true">'
        )
        assert call["user"].endswith("</beta8_revision_data>")
        assert set(request_payload(call)) == expected_user_keys
    assert len(publisher.bundles) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_revision_markdown_is_validated_before_checkpoint(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, revise=True, invalid_revision_markdown_once=True
    )

    await runner.run("version-1", "worker-1")

    calls = [
        call for call in provider.calls if call["scene_id"] == "targeted_revision"
    ]
    assert len(calls) == 2
    assert "Markdown 结构校验" in calls[1]["system"]
    expected_user_keys = {
        "source_cards", "revision_task", "transcript_segments", "search_packets",
    }
    for call in calls:
        assert call["user"].endswith("</beta8_revision_data>")
        assert set(request_payload(call)) == expected_user_keys
    assert publisher.bundles[0].cards[0].summary == "完成修订后的核心摘要。"
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_metrics_count_every_actual_model_call_once(tmp_path) -> None:
    database, provider, _, runner = await harness(
        tmp_path, v1_error_code="network_timeout"
    )
    with pytest.raises(ProviderAnalysisError, match="V1 provider failure"):
        await runner.run("version-1", "worker-1")
    await runner.run("version-1", "worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert metrics["model_call_count"] == len(provider.calls)
    assert metrics["new_model_call_count"] == len([
        item for item in metrics["model_calls"]
        if item["run_id"] == metrics["run_id"]
    ])
    assert metrics["historical_model_call_count"] == 2
    assert len({item["run_id"] for item in metrics["model_calls"]}) == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_metrics_separate_v1_repairs_audits_and_revisions_by_run(
    tmp_path,
) -> None:
    database, _, _, runner = await harness(
        tmp_path,
        invalid_v1_schema_failures=1,
        revise=True,
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    calls = metrics["model_calls"]
    assert [(item["stage"], item["attempt_kind"]) for item in calls[:3]] == [
        ("event_index", "normal"),
        ("all_scenes_v1", "normal"),
        ("all_scenes_v1", "schema_repair"),
    ]
    assert {item["run_id"] for item in calls} == {metrics["run_id"]}
    assert {item["stage"] for item in calls}.issuperset({
        "initial_audit", "editorial_review", "revisions", "final_audit",
    })
    assert metrics["stage_durations_ms"].keys() >= {
        "event_index", "all_scenes_v1", "initial_audit", "editorial_review",
        "search", "revisions", "final_audit",
    }
    assert metrics["run_duration_ms"] >= 0
    assert metrics["run_started_at"]
    assert metrics["run_finished_at"]
    assert metrics["final_audit_score"] == 100
    assert all(item["cost"] is None for item in calls)
    assert all(item["cost_unavailable_reason"] for item in calls)
    await database.dispose()


@pytest.mark.asyncio
async def test_parallel_audit_diagnostics_are_each_attributed_once(tmp_path) -> None:
    database, _, _, runner = await harness(
        tmp_path, block_audits=True, retry_scene_once="unified_audit"
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    audit_calls = [
        item for item in metrics["model_calls"]
        if item["stage"] in {"initial_audit", "final_audit"}
    ]
    assert len(audit_calls) == 5
    assert all(item["input_tokens"] == 100 for item in audit_calls)
    assert [item["attempt_kind"] for item in audit_calls].count("retry") == 1
    assert len({item["invocation_id"] for item in audit_calls}) == 4
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pipeline_kind", "provider_kwargs", "expected_stage"),
    [
        ("beta8_indexed_scene_v2", {"fail_any_scene_once": "unified_audit"}, "initial_audit"),
        ("beta8_indexed_scene_v2", {"fail_any_scene_once": "cross_card_orchestration"}, "editorial_review"),
        ("beta8_indexed_scene_v2", {"fail_any_scene_once": "targeted_revision", "revise": True}, "revisions"),
        ("beta8_multi_scene_v1", {"fail_scene": "work_communication"}, None),
    ],
)
async def test_failed_downstream_run_finalizes_metrics_once(
    tmp_path, pipeline_kind, provider_kwargs, expected_stage
) -> None:
    database, provider, _, runner = await harness(
        tmp_path,
        pipeline_kind=pipeline_kind,
        runner_class=FailureBoundaryRunner,
        **provider_kwargs,
    )

    with pytest.raises(RuntimeError, match="forced failure|scene failed"):
        await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert runner.metric_saves == 1
    assert metrics["run_finished_at"]
    assert metrics["new_model_call_count"] == len(provider.request_diagnostics)
    if expected_stage is not None:
        assert expected_stage in metrics["stage_durations_ms"]
    await database.dispose()


@pytest.mark.asyncio
async def test_search_failure_finalizes_stage_duration_and_metrics(tmp_path) -> None:
    database, _, _, runner = await harness(
        tmp_path, runner_class=FailureBoundaryRunner
    )
    runner.fail_search = True

    with pytest.raises(RuntimeError, match="after search"):
        await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert runner.metric_saves == 1
    assert metrics["run_finished_at"]
    assert "search" in metrics["stage_durations_ms"]
    await database.dispose()


@pytest.mark.asyncio
async def test_final_requirement_failure_finalizes_metrics(tmp_path) -> None:
    database, _, _, runner = await harness(
        tmp_path, runner_class=FailureBoundaryRunner
    )
    runner.fail_final_requirements = True

    with pytest.raises(RuntimeError, match="final requirement"):
        await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert runner.metric_saves == 1
    assert metrics["run_finished_at"]
    assert "final_audit" in metrics["stage_durations_ms"]
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_kwargs", "expected_kinds"),
    [
        ({"truncate_scene_once": "work_communication"}, ["normal", "retry"]),
        ({"invalid_scene_once": "work_communication"}, ["normal", "schema_repair"]),
    ],
)
async def test_legacy_repairs_are_not_counted_as_normal_generation(
    tmp_path, provider_kwargs, expected_kinds
) -> None:
    database, _, _, runner = await harness(
        tmp_path,
        pipeline_kind="beta8_multi_scene_v1",
        **provider_kwargs,
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    matching = [
        item["attempt_kind"] for item in metrics["model_calls"]
        if item["stage"] == "work_communication"
    ]
    assert matching == expected_kinds
    await database.dispose()


@pytest.mark.asyncio
async def test_unknown_provider_tokens_remain_null_through_persistence(tmp_path) -> None:
    database, _, _, runner = await harness(
        tmp_path, token_usage_available=False
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert metrics["run_input_tokens"] is None
    assert metrics["run_output_tokens"] is None
    assert metrics["input_tokens"] is None
    assert metrics["output_tokens"] is None
    assert metrics["token_usage_unavailable_reason"]
    assert all(item["input_tokens"] is None for item in metrics["model_calls"])
    await database.dispose()


@pytest.mark.asyncio
async def test_reused_runner_does_not_finalize_missing_version_with_stale_state(
    tmp_path,
) -> None:
    database, _, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        original_metrics = version.pipeline_metrics_json

    with pytest.raises(LookupError, match="Unknown analysis version: missing"):
        await runner.run("missing", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version.pipeline_metrics_json == original_metrics
    await database.dispose()


@pytest.mark.asyncio
async def test_reused_runner_preserves_non_running_transition_error(tmp_path) -> None:
    database, _, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        original_metrics = version.pipeline_metrics_json
        version.status = "cancelled"
        await session.commit()

    with pytest.raises(ValueError, match="not running: cancelled"):
        await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version.pipeline_metrics_json == original_metrics
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("malformed_metrics", "error_match"),
    [
        ("[]", "pipeline metrics must be an object"),
        ("{", "pipeline metrics must be valid JSON"),
    ],
)
async def test_reused_runner_does_not_overwrite_malformed_preinit_metrics(
    tmp_path, malformed_metrics, error_match
) -> None:
    database, _, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        version.pipeline_metrics_json = malformed_metrics
        await session.commit()

    with pytest.raises(ValueError, match=error_match):
        await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version.pipeline_metrics_json == malformed_metrics
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_metrics", "error_path"),
    [
        ('{ "model_call_count": -1 }', "model_call_count"),
        (
            '{ "model_calls": [{"stage":"event_index","duration_ms":-1}] }',
            "model_calls.0.duration_ms",
        ),
    ],
)
async def test_reused_runner_preserves_schema_invalid_metrics_object(
    tmp_path, invalid_metrics, error_path
) -> None:
    database, _, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        version.pipeline_metrics_json = invalid_metrics
        await session.commit()

    with pytest.raises(ValueError, match=error_path):
        await runner.run("version-1", "worker-1")

    assert runner._run_id == ""
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version.pipeline_metrics_json == invalid_metrics
    await database.dispose()


@pytest.mark.asyncio
async def test_reused_runner_preserves_metrics_object_with_unknown_field(
    tmp_path,
) -> None:
    database, _, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    invalid_metrics = '{ "model_call_count": 0, "future_metric": 1 }'
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        version.pipeline_metrics_json = invalid_metrics
        await session.commit()

    with pytest.raises(ValueError, match="future_metric"):
        await runner.run("version-1", "worker-1")

    assert runner._run_id == ""
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version.pipeline_metrics_json == invalid_metrics
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_persists_native_search_usage_separately(tmp_path) -> None:
    database, _, _, runner = await harness(
        tmp_path,
        native_usage_once={
            "input_tokens": 300,
            "output_tokens": 70,
            "response_count": 2,
            "tool_call_count": 1,
        },
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert metrics["search_input_tokens"] == 300
    assert metrics["search_output_tokens"] == 70
    assert metrics["search_model_response_count"] == 2
    assert metrics["web_search_tool_call_count"] == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_persists_unknown_native_search_tokens_without_losing_counts(
    tmp_path,
) -> None:
    database, _, _, runner = await harness(
        tmp_path,
        native_usage_once={
            "input_tokens": None,
            "output_tokens": None,
            "token_usage_unavailable_reason": (
                "native search provider response omitted usage"
            ),
            "response_count": 2,
            "tool_call_count": 1,
        },
    )

    await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    metrics = json.loads(version.pipeline_metrics_json)
    assert metrics["search_input_tokens"] is None
    assert metrics["search_output_tokens"] is None
    assert metrics["run_search_input_tokens"] is None
    assert metrics["run_search_output_tokens"] is None
    assert metrics["search_token_usage_unavailable_reason"] == (
        "native search provider response omitted usage"
    )
    assert metrics["search_model_response_count"] == 2
    assert metrics["web_search_tool_call_count"] == 1
    assert metrics["run_search_model_response_count"] == 2
    assert metrics["run_web_search_tool_call_count"] == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_aggregates_without_merge_model_call(tmp_path) -> None:
    database, provider, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    assert "audit_merge" not in [call["scene_id"] for call in provider.calls]
    assert [call["scene_id"] for call in provider.calls].count("cross_card_orchestration") == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_keep_card_final_hash_equals_v1_hash(tmp_path) -> None:
    database, _, publisher, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    assert all(card.untouched for card in publisher.bundles[0].cards)
    assert all(card.v1_markdown_sha256 == card.final_markdown_sha256 for card in publisher.bundles[0].cards)
    assert publisher.bundles[0].expected_work_communication_unit_ids == [
        "communication-1"
    ]
    assert publisher.bundles[0].cards[0].work_communication_unit_ids == [
        "communication-1"
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_keep_card_is_never_sent_to_revision_model(tmp_path) -> None:
    database, provider, _, runner = await harness(tmp_path)
    await runner.run("version-1", "worker-1")
    assert "targeted_revision" not in [call["scene_id"] for call in provider.calls]
    await database.dispose()


@pytest.mark.asyncio
async def test_revision_receives_target_scene_golden_prompt(tmp_path) -> None:
    database, provider, publisher, runner = await harness(tmp_path, revise=True)
    await runner.run("version-1", "worker-1")
    call = next(call for call in provider.calls if call["scene_id"] == "targeted_revision")
    target_scene_prompt = Beta8PromptComposer()._read("work_communication")
    user_payload = request_payload(call)

    assert "## 目标场景完整能力：work_communication" in call["system"]
    assert target_scene_prompt in call["system"]
    assert "target_scene_prompt" not in user_payload
    assert set(user_payload) == {
        "source_cards", "revision_task", "transcript_segments", "search_packets",
    }
    assert all(
        "work_communication_unit_ids" not in source_card
        for source_card in user_payload["source_cards"]
    )
    assert target_scene_prompt not in call["user"]
    assert publisher.bundles[0].cards[0].untouched is False
    await database.dispose()


@pytest.mark.asyncio
async def test_internal_work_unit_ids_never_enter_any_model_payload(tmp_path) -> None:
    database, provider, publisher, runner = await harness(tmp_path, revise=True)

    await runner.run("version-1", "worker-1")

    model_stages = {
        "unified_audit", "cross_card_orchestration", "targeted_revision"
    }
    calls = [call for call in provider.calls if call["scene_id"] in model_stages]
    assert {call["scene_id"] for call in calls} == model_stages
    assert [call["scene_id"] for call in calls].count("unified_audit") >= 2
    for call in calls:
        serialized = str(call["system"]) + "\n" + str(call["user"])
        assert "work_communication_unit_ids" not in serialized
        assert "communication-1" not in serialized
    assert all(
        "communication-1" not in card.markdown for card in publisher.bundles[0].cards
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_malicious_internal_work_unit_id_in_free_text_fails_closed(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, revise=True, leak_internal_id_in_orchestration=True
    )

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner.run("version-1", "worker-1")

    assert "targeted_revision" not in [call["scene_id"] for call in provider.calls]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leak_kind",
    ["candidate_id", "todo_reserved_name", "markdown_reserved_name"],
)
async def test_v1_downstream_privacy_leaks_fail_before_checkpoint_or_next_model(
    tmp_path, leak_kind,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, v1_privacy_leak=leak_kind
    )

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner.run("version-1", "worker-1")

    staged = await load_staged(database)
    assert "beta8_all_scenes_v1" not in staged
    assert not any(
        call["scene_id"] in {
            "unified_audit", "cross_card_orchestration", "targeted_revision"
        }
        for call in provider.calls
    )
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_v1_raw_response_is_quarantined_before_privacy_rejection(
    tmp_path,
) -> None:
    captured: list[tuple[str, str]] = []
    database, provider, publisher, runner = await harness(
        tmp_path,
        v1_privacy_leak="candidate_id",
        response_quarantine=lambda stage, raw: captured.append((stage, raw)),
    )

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner.run("version-1", "worker-1")

    assert [stage for stage, _raw in captured] == ["event_index", "all_scenes_v1"]
    captured_payload = json.loads(captured[1][1])
    assert captured_payload["scene_results"][0]["cards"][0]["draft_card_key"] == (
        "work_communication-1"
    )
    assert "communication-1" in captured_payload["scene_results"][0]["cards"][0][
        "search_candidates"
    ][0]["question"]
    staged = await load_staged(database)
    assert "beta8_all_scenes_v1" not in staged
    assert not any(
        call["scene_id"] == "unified_audit" for call in provider.calls
    )
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_audit_repair_privacy_leak_is_not_persisted_or_forwarded(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, invalid_audit_once=True, audit_repair_privacy_leak=True
    )

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner.run("version-1", "worker-1")

    staged = await load_staged(database)
    assert "beta8_initial_audit_aggregate" not in staged
    assert "cross_card_orchestration" not in [
        call["scene_id"] for call in provider.calls
    ]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_orchestration_repair_privacy_leak_is_not_replayed_or_persisted(
    tmp_path,
) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path,
        invalid_orchestration_once=True,
        orchestration_repair_privacy_leak=True,
    )

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner.run("version-1", "worker-1")

    orchestration_calls = [
        call for call in provider.calls
        if call["scene_id"] == "cross_card_orchestration"
    ]
    assert len(orchestration_calls) == 2
    assert "communication-1" not in str(orchestration_calls[-1]["user"])
    assert r"\u0063ommunication-1" not in str(orchestration_calls[-1]["user"])
    staged = await load_staged(database)
    assert "beta8_orchestration" not in staged
    assert "targeted_revision" not in [call["scene_id"] for call in provider.calls]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_unicode_escaped_audit_leak_writes_zero_audit_checkpoints(tmp_path) -> None:
    database, provider, publisher, runner = await harness(
        tmp_path, audit_unicode_privacy_leak=True
    )

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner.run("version-1", "worker-1")

    staged = await load_staged(database)
    assert "beta8_initial_audit_units" not in staged
    assert "beta8_initial_audit_aggregate" not in staged
    assert "cross_card_orchestration" not in [
        call["scene_id"] for call in provider.calls
    ]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_transcript_collision_fails_before_audit_model_request(tmp_path) -> None:
    database, provider, publisher, runner = await harness(tmp_path)
    async with database.session() as session:
        transcript = await session.get(Transcript, "transcript-1")
        transcript.text = "用户原话恰好包含 communication-1"
        await session.commit()

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        await runner.run("version-1", "worker-1")

    audit_calls = [
        call for call in provider.calls if call["scene_id"] == "unified_audit"
    ]
    assert all("communication-1" not in call["user"] for call in audit_calls)
    assert "cross_card_orchestration" not in [
        call["scene_id"] for call in provider.calls
    ]
    assert publisher.bundles == []
    await database.dispose()


@pytest.mark.asyncio
async def test_any_final_issue_prevents_publication(tmp_path) -> None:
    database, _, publisher, runner = await harness(tmp_path, final_issue=True)
    with pytest.raises(ValueError, match="final audit"):
        await runner.run("version-1", "worker-1")
    assert publisher.bundles == []
    await database.dispose()
