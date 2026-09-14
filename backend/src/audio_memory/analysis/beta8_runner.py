from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import time
from typing import Any, Sequence
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, update

from audio_memory.analysis.beta8_audit import (
    aggregate_audit_results,
    plan_audit_units,
)
from audio_memory.analysis.beta8_state import (
    Beta8StageRecord,
    CheckpointNotReusableError,
    canonical_hash,
    validate_stage_record,
)
from audio_memory.analysis.beta8_event_index import (
    normalize_event_index_draft,
)
from audio_memory.analysis.beta8_evaluation import final_audit_score
from audio_memory.analysis.beta8_quality import validate_v1_structure
from audio_memory.analysis.beta8_privacy import (
    ReservedMappingPrivacyError,
    project_unified_v1_privacy_surface,
    sanitized_unified_v1_repair_output,
    validate_model_request_privacy,
    validate_reserved_mapping_privacy,
)
from audio_memory.analysis.beta8_search import (
    project_search_policy,
    search_packets_for_revision_task,
)
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.analysis.full_transcript import build_full_transcript_markdown
from audio_memory.analysis.pipeline_state import ModelCallMetric, PipelineMetrics
from audio_memory.analysis.pipeline_identity import (
    BETA8_PIPELINE_KINDS,
    validate_version_pipeline_identity,
)
from audio_memory.analysis.runner import CredentialChangedError, FixedRulesChangedError, LeaseLostError
from audio_memory.db import Database
from audio_memory.models import AnalysisVersion, JobFile, Transcript
from audio_memory.prompts.beta8_composer import Beta8PromptComposer, SCENE_IDS
from audio_memory.prompts.beta8_event_index_schema import (
    Beta8EventIndexDraft,
    Beta8NormalizedEventIndex,
)
from audio_memory.prompts.beta8_pipeline_schema import (
    Beta8AuditResult,
    Beta8FinalCard,
    Beta8OrchestrationResult,
    Beta8PublicationBundle,
    Beta8RevisedCard,
    Beta8SearchPacket,
    build_strict_source_registry,
    normalize_search_packet,
    validate_orchestration_against_inputs,
    validate_publication_bundle,
)
from audio_memory.prompts.beta8_scene_schema import (
    Beta8SceneResult,
    Beta8UnifiedV1Result,
    normalize_uniquely_misindexed_segment_ids,
    parse_beta8_card_markdown,
    validate_scene_result_ids,
    validate_unified_v1,
)


def _normalize_audit_result_payload(payload: object) -> object:
    """Discard a scene hint when the issue already targets an existing card."""
    if not isinstance(payload, dict):
        return payload
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload

    normalized = deepcopy(payload)
    for issue in normalized["issues"]:
        if (
            isinstance(issue, dict)
            and issue.get("issue_type") != "missed_high_value_content"
            and issue.get("suggested_scene_id") is not None
        ):
            issue["suggested_scene_id"] = None
    return normalized


def _normalize_unified_v1_card_todos(payload: object) -> object:
    """Move only misnested card todo arrays to their containing scene."""
    if not isinstance(payload, dict):
        return payload
    scene_results = payload.get("scene_results")
    if not isinstance(scene_results, list):
        return payload

    found_nested = False
    for scene in scene_results:
        if not isinstance(scene, dict):
            return payload
        scene_todos = scene.get("todo_candidates", [])
        cards = scene.get("cards")
        if not isinstance(scene_todos, list) or not isinstance(cards, list):
            return payload
        for card in cards:
            if not isinstance(card, dict):
                return payload
            if "todo_candidates" not in card:
                continue
            if not isinstance(card["todo_candidates"], list):
                return payload
            found_nested = True
    if not found_nested:
        return payload

    normalized = deepcopy(payload)
    for scene in normalized["scene_results"]:
        scene_todos = scene.setdefault("todo_candidates", [])
        for card in scene["cards"]:
            scene_todos.extend(card.pop("todo_candidates", []))
    return normalized


def _normalize_unified_v1_segment_ids(
    result: Beta8UnifiedV1Result, *, known_segment_ids: set[str]
) -> Beta8UnifiedV1Result:
    payload = result.model_dump(mode="json")
    for scene_index, scene in enumerate(result.scene_results):
        projected = Beta8SceneResult.model_validate({
            "cards": [
                {
                    "markdown": card.markdown,
                    "source_segment_ids": card.source_segment_ids,
                    "search_candidates": [
                        candidate.model_dump(mode="json")
                        for candidate in card.search_candidates
                    ],
                }
                for card in scene.cards
            ],
            "todo_candidates": [
                candidate.model_dump(mode="json")
                for candidate in scene.todo_candidates
            ],
            "skip_reason": scene.skip_reason,
        })
        corrected = normalize_uniquely_misindexed_segment_ids(
            projected, known_segment_ids=known_segment_ids
        )
        for card_index, card in enumerate(corrected.cards):
            target = payload["scene_results"][scene_index]["cards"][card_index]
            target["source_segment_ids"] = card.source_segment_ids
            target["search_candidates"] = [
                candidate.model_dump(mode="json")
                for candidate in card.search_candidates
            ]
        payload["scene_results"][scene_index]["todo_candidates"] = [
            candidate.model_dump(mode="json")
            for candidate in corrected.todo_candidates
        ]
    return Beta8UnifiedV1Result.model_validate(payload)


def _normalize_unified_v1_communication_kinds(payload: object) -> object:
    """Default only omitted work-communication metadata to the valid catch-all."""
    if not isinstance(payload, dict):
        return payload
    scene_results = payload.get("scene_results")
    if not isinstance(scene_results, list):
        return payload

    normalized = None
    for scene_index, scene in enumerate(scene_results):
        if not isinstance(scene, dict) or not isinstance(scene.get("cards"), list):
            continue
        for card_index, card in enumerate(scene["cards"]):
            if not isinstance(card, dict):
                continue
            basis = card.get("card_basis")
            if not isinstance(basis, dict):
                continue
            if basis.get("type") != "work_communication":
                continue
            if basis.get("communication_kind") is not None:
                continue
            if normalized is None:
                normalized = deepcopy(payload)
            normalized["scene_results"][scene_index]["cards"][card_index][
                "card_basis"
            ]["communication_kind"] = "other"
    return normalized if normalized is not None else payload


_EMPTY_HASH = sha256(b"").hexdigest()
_SCENE_CONVERGENCE_INSTRUCTIONS = (
    "\n\n篇幅收敛要求：生成完整、合法的 JSON，不得省略任何具有独立用户价值的主题；"
    "在不损失关键事实、判断和帮助的前提下，优先合并重复事实与证据片段引用，"
    "避免复述逐字稿、重复建议和低价值日常细节。默认按场景 Prompt 的卡片合并原则"
    "收敛篇幅，并确保 JSON 正常闭合。"
)


class Beta8CheckpointPause(RuntimeError):
    code = "intentional_v1_checkpoint_pause"

    def __init__(
        self,
        message: str,
        *,
        checkpoint_stage: str = "all_scenes_v1",
    ) -> None:
        super().__init__(message)
        self.checkpoint_stage = checkpoint_stage
        if checkpoint_stage == "event_index":
            self.code = "intentional_event_index_checkpoint_pause"


@dataclass(slots=True)
class _RunContext:
    run_id: str | None = None


class Beta8ReportRunner:
    def __init__(
        self,
        *,
        database: Database,
        provider,
        publisher,
        generation_source,
        search_executor,
        stop_after_event_index_checkpoint: bool = False,
        stop_after_v1_checkpoint: bool = False,
        response_quarantine=None,
        event_index_gate=None,
    ) -> None:
        self.database = database
        self.provider = provider
        self.publisher = publisher
        self.generation_source = generation_source
        self.search_executor = search_executor
        self.stop_after_event_index_checkpoint = stop_after_event_index_checkpoint
        self.stop_after_v1_checkpoint = stop_after_v1_checkpoint
        self.response_quarantine = response_quarantine
        self.event_index_gate = event_index_gate
        self.composer = Beta8PromptComposer()
        self._run_lock = asyncio.Lock()
        self._checkpoint_lock = asyncio.Lock()
        self._model_calls = 0
        self._model_duration_ms = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._web_search_performed = False
        self._web_search_degraded_reason: str | None = None
        self._run_search_provider_id: str | None = None
        self._search_usage_base = {
            "input_tokens": 0,
            "output_tokens": 0,
            "response_count": 0,
            "tool_call_count": 0,
        }
        self._search_usage_offset = dict(self._search_usage_base)
        self._search_token_usage_unavailable_reasons: set[str] = set()
        self._provider_generation = 0
        self._run_id = ""
        self._run_started_at = ""
        self._run_finished_at: str | None = None
        self._run_started_monotonic = 0.0
        self._run_model_duration_ms = 0
        self._run_input_tokens = 0
        self._run_output_tokens = 0
        self._historical_model_call_count = 0
        self._model_call_metrics: list[ModelCallMetric] = []
        self._stage_durations_ms: dict[str, int] = {}
        self._checkpoint_reused_stages: set[str] = set()
        self._final_audit_score: int | None = None
        self._resumed_run = False
        self._historical_total_duration_ms = 0
        self._input_tokens_known = True
        self._output_tokens_known = True
        self._run_input_tokens_known = True
        self._run_output_tokens_known = True
        self._token_usage_unavailable_reasons: set[str] = set()

    async def run(self, version_id: str, worker_owner_id: str | None = None):
        async with self._run_lock:
            return await self._run_serialized(version_id, worker_owner_id)

    async def _run_serialized(
        self, version_id: str, worker_owner_id: str | None = None
    ):
        context = _RunContext()
        self._clear_run_state()
        try:
            return await self._run_once(version_id, worker_owner_id, context)
        except BaseException as error:
            if context.run_id is None or self._run_id != context.run_id:
                raise
            if isinstance(error, LeaseLostError):
                raise
            self._run_finished_at = datetime.now(timezone.utc).isoformat()
            try:
                await self._save_metrics(version_id, None, worker_owner_id)
            except LeaseLostError:
                if await self._metrics_fence_proves_owner_loss(
                    version_id, worker_owner_id
                ):
                    raise
            except Exception:
                pass
            raise

    async def _metrics_fence_proves_owner_loss(
        self, version_id: str, worker_owner_id: str | None
    ) -> bool:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        return version is None or (
            worker_owner_id is not None
            and version.worker_owner_id != worker_owner_id
        )

    def _clear_run_state(self) -> None:
        self._model_calls = 0
        self._model_duration_ms = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._web_search_performed = False
        self._web_search_degraded_reason = None
        self._run_search_provider_id = None
        self._search_usage_base = {
            "input_tokens": 0,
            "output_tokens": 0,
            "response_count": 0,
            "tool_call_count": 0,
        }
        self._search_usage_offset = dict(self._search_usage_base)
        self._search_token_usage_unavailable_reasons = set()
        self._provider_generation = 0
        self._run_id = ""
        self._run_started_at = ""
        self._run_finished_at = None
        self._run_started_monotonic = 0.0
        self._run_model_duration_ms = 0
        self._run_input_tokens = 0
        self._run_output_tokens = 0
        self._historical_model_call_count = 0
        self._model_call_metrics = []
        self._stage_durations_ms = {}
        self._checkpoint_reused_stages = set()
        self._final_audit_score = None
        self._resumed_run = False
        self._historical_total_duration_ms = 0
        self._input_tokens_known = True
        self._output_tokens_known = True
        self._run_input_tokens_known = True
        self._run_output_tokens_known = True
        self._token_usage_unavailable_reasons = set()

    async def _run_once(
        self,
        version_id: str,
        worker_owner_id: str | None,
        context: _RunContext,
    ):
        version = await self._version(version_id, worker_owner_id)
        identity = validate_version_pipeline_identity(version)
        if identity.pipeline_kind not in BETA8_PIPELINE_KINDS:
            raise ValueError(
                f"Unknown Beta 8 report pipeline: {identity.pipeline_kind}"
            )
        search_executor = self.search_executor.with_binding(
            identity.search_provider_id,
            identity.search_model_id,
        )
        self._run_search_provider_id = search_executor.provider_id
        try:
            persisted_metrics = json.loads(version.pipeline_metrics_json or "{}")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("Beta 8 pipeline metrics must be valid JSON") from error
        if not isinstance(persisted_metrics, dict):
            raise ValueError("Beta 8 pipeline metrics must be an object")
        validated_metrics = PipelineMetrics.model_validate(persisted_metrics)
        self._model_calls = int(persisted_metrics.get("model_call_count", 0) or 0)
        self._model_duration_ms = int(persisted_metrics.get("model_duration_ms", 0) or 0)
        persisted_input_tokens = persisted_metrics.get("input_tokens", 0)
        persisted_output_tokens = persisted_metrics.get("output_tokens", 0)
        self._input_tokens_known = persisted_input_tokens is not None
        self._output_tokens_known = persisted_output_tokens is not None
        self._input_tokens = int(persisted_input_tokens or 0)
        self._output_tokens = int(persisted_output_tokens or 0)
        self._historical_total_duration_ms = int(
            persisted_metrics.get("total_duration_ms", 0) or 0
        )
        self._web_search_performed = bool(
            persisted_metrics.get("web_search_performed", False)
        )
        self._web_search_degraded_reason = persisted_metrics.get(
            "web_search_degraded_reason"
        )
        persisted_search_input = persisted_metrics.get("search_input_tokens", 0)
        persisted_search_output = persisted_metrics.get("search_output_tokens", 0)
        self._search_usage_base = {
            "input_tokens": (
                None
                if persisted_search_input is None
                else int(persisted_search_input)
            ),
            "output_tokens": (
                None
                if persisted_search_output is None
                else int(persisted_search_output)
            ),
            "response_count": int(
                persisted_metrics.get("search_model_response_count", 0) or 0
            ),
            "tool_call_count": int(
                persisted_metrics.get("web_search_tool_call_count", 0) or 0
            ),
        }
        self._search_usage_offset = self._native_search_usage_snapshot()
        persisted_search_reason = persisted_metrics.get(
            "search_token_usage_unavailable_reason"
        )
        self._search_token_usage_unavailable_reasons = (
            {str(persisted_search_reason)} if persisted_search_reason else set()
        )
        self._run_id = str(uuid4())
        self._run_started_at = datetime.now(timezone.utc).isoformat()
        self._run_finished_at = None
        self._run_started_monotonic = time.monotonic()
        self._run_model_duration_ms = 0
        self._run_input_tokens = 0
        self._run_output_tokens = 0
        self._run_input_tokens_known = True
        self._run_output_tokens_known = True
        persisted_token_reason = persisted_metrics.get(
            "token_usage_unavailable_reason"
        )
        self._token_usage_unavailable_reasons = (
            {str(persisted_token_reason)} if persisted_token_reason else set()
        )
        self._historical_model_call_count = self._model_calls
        self._model_call_metrics = list(validated_metrics.model_calls)
        self._stage_durations_ms = {}
        self._checkpoint_reused_stages = set()
        self._final_audit_score = None
        self._resumed_run = bool(self._model_calls or self._load_staged(version.staged_results_json))
        self._provider_generation = version.credential_generation
        context.run_id = self._run_id
        if version.fixed_rules_hash != self.composer.fixed_rules_hash():
            raise FixedRulesChangedError("Fixed Beta 8 analysis rules changed")
        generation = await self.generation_source.credential_generation(version.provider_id)
        if generation != version.credential_generation:
            raise CredentialChangedError("Credential generation changed")
        transcript = await self._transcript(version.source_job_id)
        transcript_markdown = build_full_transcript_markdown(transcript)
        transcript_fingerprint = canonical_hash(transcript)
        known_segment_ids = {str(item["segment_id"]) for item in transcript}
        staged = self._load_staged(version.staged_results_json)

        pipeline_kind = identity.pipeline_kind
        expected_work_communication_unit_ids: list[str] = []
        if pipeline_kind == "beta8_multi_scene_v1":
            scene_outputs = await self._run_scenes(
                version, staged, transcript_markdown, transcript_fingerprint,
                known_segment_ids, worker_owner_id,
            )
        elif pipeline_kind == "beta8_indexed_scene_v2":
            event_index = await self._timed_stage(
                "event_index",
                self._run_event_index(
                    version, staged, transcript, transcript_markdown,
                    transcript_fingerprint, worker_owner_id,
                ),
            )
            expected_work_communication_unit_ids = sorted(
                event_index.work_communication_session_ids
            )
            if self.stop_after_event_index_checkpoint:
                raise Beta8CheckpointPause(
                    "Paused after saving the event_index checkpoint",
                    checkpoint_stage="event_index",
                )
            scene_outputs = await self._timed_stage(
                "all_scenes_v1",
                self._run_unified_v1(
                    version, staged, transcript_markdown, event_index,
                    transcript_fingerprint, known_segment_ids, worker_owner_id,
                ),
            )
            if self.stop_after_v1_checkpoint:
                raise Beta8CheckpointPause(
                    "Paused after saving the all_scenes_v1 checkpoint"
                )
        else:
            raise ValueError(f"Unknown Beta 8 report pipeline: {pipeline_kind}")
        cards, search_candidates, todo_candidates = self._materialize_scene_outputs(
            scene_outputs
        )
        work_unit_ids_by_card_id = self._work_unit_ids_by_card_id(scene_outputs)
        cards_hash = canonical_hash(cards)
        initial_audit = await self._timed_stage(
            "initial_audit",
            self._run_audit(
                version=version, staged=staged, transcript=transcript, cards=cards,
                phase="initial", requirements=[], transcript_fingerprint=transcript_fingerprint,
                upstream_hash=cards_hash, worker_owner_id=worker_owner_id,
                internal_work_unit_ids=expected_work_communication_unit_ids,
            ),
        )
        if initial_audit.incomplete_audit_unit_ids:
            raise ValueError("initial audit is incomplete")
        orchestration = await self._timed_stage(
            "editorial_review",
            self._run_orchestration(
                version=version, staged=staged, cards=cards, audit=initial_audit,
                search_candidates=search_candidates, todo_candidates=todo_candidates,
                transcript_fingerprint=transcript_fingerprint,
                upstream_hash=initial_audit.model_dump(mode="json"),
                worker_owner_id=worker_owner_id,
                work_unit_ids_by_card_id=work_unit_ids_by_card_id,
            ),
        )
        if orchestration.unresolved_conflicts:
            raise ValueError("unresolved orchestration conflict prevents publication")
        project_search_policy(
            search_candidates=search_candidates,
            orchestration=orchestration,
        )

        packets = await self._timed_stage(
            "search",
            self._run_search(
                version, staged, orchestration, transcript_fingerprint,
                canonical_hash(orchestration.model_dump(mode="json")), worker_owner_id,
                search_executor, work_unit_ids_by_card_id,
            ),
        )
        final_cards = await self._timed_stage(
            "revisions",
            self._run_revisions(
                version=version, staged=staged, cards=cards, orchestration=orchestration,
                transcript=transcript, packets=packets,
                transcript_fingerprint=transcript_fingerprint,
                upstream_hash=canonical_hash([packet.model_dump(mode="json") for packet in packets]),
                worker_owner_id=worker_owner_id,
                work_unit_ids_by_card_id=work_unit_ids_by_card_id,
            ),
        )
        external_sources = build_strict_source_registry(tuple(packets))
        bundle = Beta8PublicationBundle.model_validate({
            "cards": [card.model_dump(mode="json") for card in final_cards],
            "todo_candidates": [item.model_dump(mode="json") for item in orchestration.todo_candidates],
            "external_sources": [source.model_dump(mode="json") for source in external_sources],
            "v1_card_hashes": {
                card.card_id: card.v1_markdown_sha256
                for card in final_cards if card.v1_markdown_sha256 is not None
            },
            "final_card_hashes": {
                card.card_id: card.final_markdown_sha256 for card in final_cards
            },
            "untouched_card_ids": [card.card_id for card in final_cards if card.untouched],
            "completed_revision_task_ids": [
                task.revision_task_key for task in orchestration.revision_tasks
            ],
            "search_degraded": any(packet.status != "success" for packet in packets),
            "search_degraded_reason": self._search_degraded_reason(packets),
            "expected_work_communication_unit_ids": expected_work_communication_unit_ids,
        })
        validate_publication_bundle(bundle)
        candidate_hash = canonical_hash(bundle.model_dump(mode="json"))
        await self._save_stage(
            version.id, staged, "beta8_final_candidate",
            bundle.model_dump(mode="json"), transcript_fingerprint,
            candidate_hash, worker_owner_id, upstream_hash=canonical_hash(
                [card.model_dump(mode="json") for card in final_cards]
            ),
        )
        requirements = [
            requirement.model_dump(mode="json")
            for task in orchestration.revision_tasks for requirement in task.requirements
        ]
        final_audit = await self._timed_stage(
            "final_audit",
            self._run_audit(
                version=version, staged=staged, transcript=transcript,
                cards=[card.model_dump(mode="json") for card in final_cards],
                phase="final", requirements=requirements,
                transcript_fingerprint=transcript_fingerprint,
                upstream_hash=candidate_hash, worker_owner_id=worker_owner_id,
                internal_work_unit_ids=expected_work_communication_unit_ids,
            ),
        )
        self._final_audit_score = final_audit_score(final_audit)
        if final_audit.incomplete_audit_unit_ids or final_audit.issues:
            raise ValueError("final audit failed; publication is blocked")
        self._validate_final_requirement_checks(staged, requirements)
        await self._save_stage(
            version.id, staged, "beta8_publication_ready",
            bundle.model_dump(mode="json"), transcript_fingerprint,
            canonical_hash(final_audit.model_dump(mode="json")), worker_owner_id,
            upstream_hash=canonical_hash(final_audit.model_dump(mode="json")),
        )
        self._run_finished_at = datetime.now(timezone.utc).isoformat()
        await self._save_metrics(version.id, packets, worker_owner_id)
        return await self.publisher.publish_beta8(
            version.id, bundle, worker_owner_id=worker_owner_id
        )

    async def _run_scenes(
        self, version, staged, transcript_markdown, transcript_fingerprint,
        known_segment_ids, worker_owner_id,
    ) -> dict[str, Beta8SceneResult]:
        payload = self._stage_payload(
            staged, "beta8_scene_v1", transcript_fingerprint, _EMPTY_HASH
        ) or {}
        completed: dict[str, dict[str, Any]] = (
            dict(payload) if isinstance(payload, dict) else {}
        )

        async def generate(scene_id: str):
            request = self.composer.compose_scene(
                scene_id, transcript_markdown=transcript_markdown
            )
            if scene_id == "parenting_family" and request.segment_count >= 5_000:
                request = replace(
                    request,
                    instructions=(
                        request.instructions + _SCENE_CONVERGENCE_INSTRUCTIONS
                    ),
                )
            try:
                result = await self._generate_scene_result(
                    version, request, known_segment_ids,
                    repair_attempted=False, attempt_kind="normal",
                )
            except ProviderAnalysisError as error:
                if error.code != "model_output_truncated":
                    raise
                convergence = replace(
                    request,
                    instructions=(
                        request.instructions
                        + "\n\n上一次输出因篇幅超出模型上限而被截断。"
                        + _SCENE_CONVERGENCE_INSTRUCTIONS
                    ),
                )
                result = await self._generate_scene_result(
                    version, convergence, known_segment_ids,
                    repair_attempted=True, attempt_kind="retry",
                )
            except (ValueError, json.JSONDecodeError) as error:
                repair = replace(
                    request,
                    instructions=(
                        request.instructions
                        + "\n\n上一次输出未通过运行时契约校验。只修复下列错误，"
                        "不得改变有证据支持的实质内容：\n"
                        + str(error)
                    ),
                )
                result = await self._generate_scene_result(
                    version, repair, known_segment_ids,
                    repair_attempted=True, attempt_kind="schema_repair",
                )
            return scene_id, result

        missing = [scene_id for scene_id in SCENE_IDS if scene_id not in completed]
        outcomes = await asyncio.gather(
            *(generate(scene_id) for scene_id in missing), return_exceptions=True
        )
        first_error: BaseException | None = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                first_error = first_error or outcome
                continue
            scene_id, result = outcome
            completed[scene_id] = result.model_dump(mode="json")
            await self._save_stage(
                version.id, staged, "beta8_scene_v1", completed,
                transcript_fingerprint, self.composer.fixed_rules_hash(),
                worker_owner_id, upstream_hash=_EMPTY_HASH,
            )
        if first_error is not None:
            raise first_error
        if set(completed) != set(SCENE_IDS):
            raise ValueError("all seven Beta 8 scenes must complete")
        return {
            scene_id: Beta8SceneResult.model_validate(completed[scene_id])
            for scene_id in SCENE_IDS
        }

    async def _generate_scene_result(
        self, version, request, known_segment_ids, *,
        repair_attempted: bool, attempt_kind: str,
    ) -> Beta8SceneResult:
        raw = await self._generate(
            version,
            request,
            allow_parallel=True,
            repair_attempted=repair_attempted,
            attempt_kind=attempt_kind,
        )
        result = Beta8SceneResult.model_validate(json.loads(raw))
        result = normalize_uniquely_misindexed_segment_ids(
            result, known_segment_ids=known_segment_ids
        )
        validate_scene_result_ids(result, known_segment_ids=known_segment_ids)
        for card in result.cards:
            parse_beta8_card_markdown(card.markdown)
        return result

    async def _run_event_index(
        self, version, staged, transcript, transcript_markdown,
        transcript_fingerprint, worker_owner_id,
    ) -> Beta8NormalizedEventIndex:
        cached = self._stage_payload(
            staged, "beta8_event_index", transcript_fingerprint, _EMPTY_HASH
        )
        if cached is not None:
            index = Beta8NormalizedEventIndex.model_validate(cached)
            self._run_event_index_gate(index)
            return index

        request = self.composer.compose_event_index(
            transcript_markdown=transcript_markdown,
            max_tokens=(
                64_000 if version.model_id == "deepseek-v4-flash" else 32_000
            ),
        )
        raw = await self._generate(version, request)
        self._quarantine_response("event_index", raw)
        draft = self._parse_event_index_draft(raw)
        index = normalize_event_index_draft(draft, transcript)

        self._run_event_index_gate(index)
        payload = index.model_dump(mode="json")
        await self._save_stage(
            version.id, staged, "beta8_event_index", payload,
            transcript_fingerprint, self.composer.fixed_rules_hash(),
            worker_owner_id, upstream_hash=_EMPTY_HASH,
        )
        return index

    async def _run_unified_v1(
        self, version, staged, transcript_markdown, event_index,
        transcript_fingerprint, known_segment_ids, worker_owner_id,
    ) -> Beta8UnifiedV1Result:
        await self._version(version.id, worker_owner_id)
        index_hash = canonical_hash(event_index.model_dump(mode="json"))
        cached = self._stage_payload(
            staged, "beta8_all_scenes_v1", transcript_fingerprint, index_hash
        )
        if cached is not None:
            result = Beta8UnifiedV1Result.model_validate(cached)
            result = _normalize_unified_v1_segment_ids(
                result, known_segment_ids=known_segment_ids
            )
            self._validate_unified_v1(
                result,
                known_segment_ids=known_segment_ids,
                event_index=event_index,
            )
            return result

        request = self.composer.compose_all_scenes_v1(
            transcript_markdown=transcript_markdown,
            event_index=event_index,
        )
        raw = await self._generate(version, request)
        self._quarantine_response("all_scenes_v1", raw)
        try:
            result = self._parse_unified_v1(raw)
        except json.JSONDecodeError as error:
            if (
                self.stop_after_v1_checkpoint
                or not self._is_locally_repairable_json_error(raw, error)
            ):
                raise ProviderAnalysisError(
                    "Unified V1 response is incomplete JSON",
                    code="model_response_invalid",
                ) from error
            result = await self._repair_unified_v1(
                version, request, raw, error,
                internal_unit_ids=set(event_index.routeable_unit_ids),
            )
        except ValidationError as error:
            if (
                self.stop_after_v1_checkpoint
                or not self._is_localizable_schema_error(error)
            ):
                raise ProviderAnalysisError(
                    "Unified V1 response is structurally incomplete",
                    code="model_response_invalid",
                ) from error
            result = await self._repair_unified_v1(
                version, request, raw, error,
                internal_unit_ids=set(event_index.routeable_unit_ids),
            )

        result = _normalize_unified_v1_segment_ids(
            result, known_segment_ids=known_segment_ids
        )
        self._validate_unified_v1(
            result,
            known_segment_ids=known_segment_ids,
            event_index=event_index,
        )
        payload = result.model_dump(mode="json")
        await self._save_stage(
            version.id, staged, "beta8_all_scenes_v1", payload,
            transcript_fingerprint, self.composer.fixed_rules_hash(),
            worker_owner_id, upstream_hash=index_hash,
        )
        return result

    @staticmethod
    def _parse_event_index_draft(raw: str) -> Beta8EventIndexDraft:
        try:
            return Beta8EventIndexDraft.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as error:
            raise ProviderAnalysisError(
                "Event index draft response is invalid",
                code="model_response_invalid",
            ) from error

    @staticmethod
    def _parse_unified_v1(raw: str) -> Beta8UnifiedV1Result:
        payload = _normalize_unified_v1_card_todos(json.loads(raw))
        payload = _normalize_unified_v1_communication_kinds(payload)
        return Beta8UnifiedV1Result.model_validate(payload)

    async def _repair_unified_v1(
        self, version, request, raw, error, *, internal_unit_ids
    ):
        safe_output = sanitized_unified_v1_repair_output(
            raw, internal_unit_ids=internal_unit_ids
        )
        repair = replace(
            request,
            instructions=(
                request.instructions
                + "\n\n上一次统一 V1 输出完整，但未通过可定位的 JSON/Schema "
                "契约。只修复下列结构错误，不得删除、改写或压缩实质内容：\n"
                + str(error)
            ),
            user_data=(
                request.user_data
                + '<beta8_invalid_all_scenes_v1_output untrusted="true">'
                + safe_output.replace("</", "<\\/")
                + "</beta8_invalid_all_scenes_v1_output>"
            ),
        )
        repaired_raw = await self._generate(
            version, repair, repair_attempted=True,
            attempt_kind="schema_repair",
        )
        self._quarantine_response("all_scenes_v1_schema_repair", repaired_raw)
        try:
            return self._parse_unified_v1(repaired_raw)
        except (json.JSONDecodeError, ValidationError) as repair_error:
            raise ProviderAnalysisError(
                "Unified V1 structure repair failed",
                code="model_response_invalid",
            ) from repair_error

    def _quarantine_response(self, stage: str, raw: str) -> None:
        if self.response_quarantine is not None:
            self.response_quarantine(stage, raw)

    def _run_event_index_gate(self, index: Beta8NormalizedEventIndex) -> None:
        if self.event_index_gate is not None:
            self.event_index_gate(index)

    @staticmethod
    def _is_locally_repairable_json_error(
        raw: str, error: json.JSONDecodeError
    ) -> bool:
        """Allow only one provably sufficient top-level trailing-comma repair."""
        start = 0
        while start < len(raw) and raw[start].isspace():
            start += 1
        close = len(raw) - 1
        while close >= 0 and raw[close].isspace():
            close -= 1
        comma = close - 1
        while comma >= 0 and raw[comma].isspace():
            comma -= 1
        if (
            start >= len(raw)
            or raw[start] != "{"
            or close <= start
            or raw[close] != "}"
            or comma <= start
            or raw[comma] != ","
            or error.pos != close
            or error.msg != "Expecting property name enclosed in double quotes"
        ):
            return False
        try:
            repaired = json.loads(raw[:comma] + raw[comma + 1:])
        except json.JSONDecodeError:
            return False
        return isinstance(repaired, dict)

    @staticmethod
    def _is_localizable_schema_error(error: ValidationError) -> bool:
        errors = error.errors()
        allowed = {
            "extra_forbidden",
            "missing",
            "string_type",
            "list_type",
            "dict_type",
            "literal_error",
        }
        if not errors or len(errors) > 3:
            return False
        for item in errors:
            location = item.get("loc") or ()
            if item["type"] not in allowed or not location:
                return False
            if item["type"] == "missing" and location[0] in {
                "input_complete",
                "scene_results",
            }:
                return False
        return True

    @staticmethod
    def _validate_unified_v1(
        result: Beta8UnifiedV1Result, *, known_segment_ids, event_index
    ) -> None:
        if not result.input_complete:
            raise ProviderAnalysisError(
                f"Unified V1 input is incomplete: {result.input_error}",
                code="model_response_invalid",
            )
        validate_unified_v1(
            result,
            known_segment_ids=known_segment_ids,
            event_index=event_index,
        )
        validate_v1_structure(result)
        validate_reserved_mapping_privacy(
            project_unified_v1_privacy_surface(result),
            internal_unit_ids=set(event_index.routeable_unit_ids),
            surface="unified V1 downstream-visible output",
        )

    def _materialize_scene_outputs(self, scene_outputs):
        cards: list[dict[str, Any]] = []
        searches: list[dict[str, Any]] = []
        todos: list[dict[str, Any]] = []
        if isinstance(scene_outputs, Beta8UnifiedV1Result):
            results = scene_outputs.scene_results
        else:
            results = [scene_outputs[scene_id] for scene_id in SCENE_IDS]
        for scene_index, result in enumerate(results, start=1):
            scene_id = getattr(result, "scene_id", SCENE_IDS[scene_index - 1])
            for card_index, card in enumerate(result.cards, start=1):
                card_id = f"card-{scene_index:02d}-{card_index:02d}"
                card_payload = {
                    "card_id": card_id, "scene_id": scene_id,
                    "markdown": card.markdown,
                    "source_segment_ids": card.source_segment_ids,
                    "v1_markdown_sha256": sha256(card.markdown.encode()).hexdigest(),
                }
                cards.append(card_payload)
                for candidate_index, candidate in enumerate(card.search_candidates, start=1):
                    searches.append({
                        "search_candidate_id": f"search-candidate-{scene_index:02d}-{card_index:02d}-{candidate_index:02d}",
                        "card_id": card_id, **candidate.model_dump(mode="json"),
                    })
            for todo_index, todo in enumerate(result.todo_candidates, start=1):
                todos.append({
                    "todo_candidate_id": f"todo-candidate-{scene_index:02d}-{todo_index:02d}",
                    "scene_id": scene_id, **todo.model_dump(mode="json"),
                })
        return cards, searches, todos

    @staticmethod
    def _work_unit_ids_by_card_id(scene_outputs) -> dict[str, tuple[str, ...]]:
        if not isinstance(scene_outputs, Beta8UnifiedV1Result):
            return {}
        mapping: dict[str, tuple[str, ...]] = {}
        for scene_index, result in enumerate(scene_outputs.scene_results, start=1):
            for card_index, card in enumerate(result.cards, start=1):
                card_id = f"card-{scene_index:02d}-{card_index:02d}"
                mapping[card_id] = (
                    tuple(card.card_basis.source_unit_ids)
                    if card.card_basis.type == "work_communication"
                    else ()
                )
        return mapping

    async def _run_audit(
        self, *, version, staged, transcript, cards, phase, requirements,
        transcript_fingerprint, upstream_hash, worker_owner_id,
        internal_work_unit_ids=(),
    ):
        stage_units = f"beta8_{phase}_audit_units"
        stage_aggregate = f"beta8_{phase}_audit_aggregate"
        units = plan_audit_units(transcript, phase=phase, max_markdown_chars=70_000)
        saved = self._stage_payload(staged, stage_units, transcript_fingerprint, upstream_hash) or {}
        completed = dict(saved) if isinstance(saved, dict) else {}
        for value in completed.values():
            validate_reserved_mapping_privacy(
                value,
                internal_unit_ids=internal_work_unit_ids,
                surface=f"restored {phase} audit checkpoint",
            )

        async def audit(unit):
            unit_requirements = self._requirements_for_unit(unit, requirements)
            request = self.composer.compose_audit(
                phase=phase, scope=unit.scope, audit_unit_id=unit.audit_unit_id,
                cards=cards, transcript_segments=unit.transcript_segments,
                revision_requirements=unit_requirements,
            )
            validate_model_request_privacy(
                request,
                internal_unit_ids=internal_work_unit_ids,
                surface=f"{phase} audit request",
            )
            raw = await self._generate(
                version,
                request,
                allow_parallel=True,
                stage=f"{phase}_audit",
            )
            validate_reserved_mapping_privacy(
                raw,
                internal_unit_ids=internal_work_unit_ids,
                surface=f"{phase} audit output",
            )
            try:
                decoded = _normalize_audit_result_payload(json.loads(raw))
                validate_reserved_mapping_privacy(
                    decoded,
                    internal_unit_ids=internal_work_unit_ids,
                    surface=f"parsed {phase} audit output",
                )
                result = Beta8AuditResult.model_validate(decoded)
                validate_reserved_mapping_privacy(
                    result,
                    internal_unit_ids=internal_work_unit_ids,
                    surface=f"validated {phase} audit output",
                )
            except ReservedMappingPrivacyError:
                raise
            except (ValueError, json.JSONDecodeError) as error:
                repair = replace(
                    request,
                    instructions=(
                        request.instructions
                        + "\n\n上一次审核输出未通过运行时契约校验。只修复下列错误，"
                        "不得删除真实问题、猜测场景或改变证据结论：\n"
                        + str(error)
                    ),
                )
                validate_model_request_privacy(
                    repair,
                    internal_unit_ids=internal_work_unit_ids,
                    surface=f"{phase} audit repair request",
                )
                repaired_raw = await self._generate(
                    version,
                    repair,
                    allow_parallel=True,
                    repair_attempted=True,
                    stage=f"{phase}_audit",
                    attempt_kind="schema_repair",
                )
                validate_reserved_mapping_privacy(
                    repaired_raw,
                    internal_unit_ids=internal_work_unit_ids,
                    surface=f"{phase} audit repair output",
                )
                repaired_decoded = _normalize_audit_result_payload(json.loads(repaired_raw))
                validate_reserved_mapping_privacy(
                    repaired_decoded,
                    internal_unit_ids=internal_work_unit_ids,
                    surface=f"parsed {phase} audit repair output",
                )
                result = Beta8AuditResult.model_validate(repaired_decoded)
                validate_reserved_mapping_privacy(
                    result,
                    internal_unit_ids=internal_work_unit_ids,
                    surface=f"validated {phase} audit repair output",
                )
            if result.audit_unit_id != unit.audit_unit_id:
                raise ValueError("audit_unit_id must be returned unchanged")
            return unit.audit_unit_id, result

        missing = [unit for unit in units if unit.audit_unit_id not in completed]
        outcomes = await asyncio.gather(*(audit(unit) for unit in missing), return_exceptions=True)
        first_error = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                first_error = first_error or outcome
                continue
            unit_id, result = outcome
            completed[unit_id] = result.model_dump(mode="json")
            await self._save_stage(
                version.id, staged, stage_units, completed, transcript_fingerprint,
                self.composer.fixed_rules_hash(), worker_owner_id,
                upstream_hash=upstream_hash,
            )
        if first_error is not None:
            raise first_error
        results = [Beta8AuditResult.model_validate(completed[unit.audit_unit_id]) for unit in units]
        for result in results:
            validate_reserved_mapping_privacy(
                result,
                internal_unit_ids=internal_work_unit_ids,
                surface=f"restored {phase} audit output",
            )
        aggregate = aggregate_audit_results(
            units,
            results,
            known_card_ids={str(card["card_id"]) for card in cards},
        )
        await self._save_stage(
            version.id, staged, stage_aggregate, aggregate.model_dump(mode="json"),
            transcript_fingerprint, self.composer.fixed_rules_hash(), worker_owner_id,
            upstream_hash=canonical_hash(completed),
        )
        return aggregate

    async def _run_orchestration(
        self, *, version, staged, cards, audit, search_candidates, todo_candidates,
        transcript_fingerprint, upstream_hash, worker_owner_id,
        work_unit_ids_by_card_id=None,
    ):
        upstream_hash = canonical_hash(upstream_hash) if not isinstance(upstream_hash, str) else upstream_hash
        internal_work_unit_ids = {
            unit_id
            for unit_ids in (work_unit_ids_by_card_id or {}).values()
            for unit_id in unit_ids
        }
        saved = self._stage_payload(staged, "beta8_orchestration", transcript_fingerprint, upstream_hash)
        if saved is None:
            request = self.composer.compose_orchestration(
                cards=cards, audit=audit.model_dump(mode="json"),
                search_candidates=search_candidates, todo_candidates=todo_candidates,
            )
            request = replace(
                request,
                instructions=(
                    request.instructions
                    + "\n\n统筹输出收敛要求：每个审核问题仍须恰好消费一次，每张输入卡和每个候选"
                    "也必须按正式契约完整处理；同一卡片且修改目标兼容的问题应合并为一个"
                    " combined 决定和一项修改任务。reason、requirement instruction、"
                    "preserve_points、remove_or_avoid 与 completion_criteria 只保留最短的"
                    "可执行信息，不重复审核问题全文、卡片正文或证据原文，不因收敛而删除"
                    "任何 ID、问题、决定或必要修改。"
                ),
            )
            validate_model_request_privacy(
                request,
                internal_unit_ids=internal_work_unit_ids,
                surface="orchestration request",
            )
            missed = {
                issue.audit_issue_id for issue in audit.issues
                if issue.issue_type == "missed_high_value_content"
            }

            def parse_and_validate(value: str) -> Beta8OrchestrationResult:
                decoded = json.loads(value)
                validate_reserved_mapping_privacy(
                    decoded,
                    internal_unit_ids=internal_work_unit_ids,
                    surface="parsed orchestration output",
                )
                parsed = Beta8OrchestrationResult.model_validate(decoded)
                validate_reserved_mapping_privacy(
                    parsed,
                    internal_unit_ids=internal_work_unit_ids,
                    surface="validated orchestration output",
                )
                validate_orchestration_against_inputs(
                    parsed,
                    card_ids={card["card_id"] for card in cards},
                    audit_issue_ids={issue.audit_issue_id for issue in audit.issues},
                    search_candidate_ids={
                        item["search_candidate_id"] for item in search_candidates
                    },
                    todo_candidate_ids={
                        item["todo_candidate_id"] for item in todo_candidates
                    },
                    missed_value_issue_ids=missed,
                    work_unit_ids_by_card_id=work_unit_ids_by_card_id,
                )
                return parsed

            raw = await self._generate(
                version, request, stage="editorial_review"
            )
            validate_reserved_mapping_privacy(
                raw,
                internal_unit_ids=internal_work_unit_ids,
                surface="orchestration output",
            )
            for repair_index in range(3):
                try:
                    result = parse_and_validate(raw)
                    break
                except ReservedMappingPrivacyError:
                    raise
                except (ValueError, json.JSONDecodeError) as error:
                    if repair_index == 2:
                        raise
                    try:
                        decoded_invalid_output = json.loads(raw)
                    except json.JSONDecodeError:
                        invalid_output = None
                        error_summary = "output is not valid JSON"
                    else:
                        validate_reserved_mapping_privacy(
                            decoded_invalid_output,
                            internal_unit_ids=internal_work_unit_ids,
                            surface="parsed invalid orchestration output",
                        )
                        invalid_output = json.dumps(
                            decoded_invalid_output,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).replace("</", "<\\/")
                        error_summary = str(error)
                    repair_user_data = request.user_data
                    if invalid_output is not None:
                        repair_user_data += (
                            '<beta8_invalid_orchestration_output untrusted="true">'
                            + invalid_output
                            + "</beta8_invalid_orchestration_output>"
                        )
                    repair = replace(
                        request,
                        instructions=(
                            request.instructions
                            + "\n\n上一次统筹输出未通过运行时契约校验。请基于"
                            "下方提供的完整旧输出做最小修改，只修复下列错误；"
                            "不得删除、拒绝或遗漏任何真实审核问题，也不得改变"
                            "有依据的卡片、搜索和待办决定：\n"
                            + error_summary
                        ),
                        user_data=repair_user_data,
                    )
                    validate_model_request_privacy(
                        repair,
                        internal_unit_ids=internal_work_unit_ids,
                        surface="orchestration repair request",
                    )
                    raw = await self._generate(
                        version,
                        repair,
                        repair_attempted=True,
                        stage="editorial_review",
                        attempt_kind="schema_repair",
                    )
                    validate_reserved_mapping_privacy(
                        raw,
                        internal_unit_ids=internal_work_unit_ids,
                        surface="orchestration repair output",
                    )
            saved = result.model_dump(mode="json")
            validate_reserved_mapping_privacy(
                saved,
                internal_unit_ids=internal_work_unit_ids,
                surface="orchestration checkpoint payload",
            )
            await self._save_stage(
                version.id, staged, "beta8_orchestration", saved,
                transcript_fingerprint, self.composer.fixed_rules_hash(), worker_owner_id,
                upstream_hash=upstream_hash,
            )
        validate_reserved_mapping_privacy(
            saved,
            internal_unit_ids=internal_work_unit_ids,
            surface="restored orchestration checkpoint",
        )
        result = Beta8OrchestrationResult.model_validate(saved)
        validate_orchestration_against_inputs(
            result,
            card_ids={card["card_id"] for card in cards},
            audit_issue_ids={issue.audit_issue_id for issue in audit.issues},
            search_candidate_ids={
                item["search_candidate_id"] for item in search_candidates
            },
            todo_candidate_ids={
                item["todo_candidate_id"] for item in todo_candidates
            },
            missed_value_issue_ids={
                issue.audit_issue_id
                for issue in audit.issues
                if issue.issue_type == "missed_high_value_content"
            },
            work_unit_ids_by_card_id=work_unit_ids_by_card_id,
        )
        return result

    async def _run_search(
        self, version, staged, orchestration, transcript_fingerprint,
        upstream_hash, worker_owner_id, search_executor,
        work_unit_ids_by_card_id=None,
    ):
        internal_work_unit_ids = {
            unit_id
            for unit_ids in (work_unit_ids_by_card_id or {}).values()
            for unit_id in unit_ids
        }
        saved = self._stage_payload(
            staged, "beta8_search_packets", transcript_fingerprint, upstream_hash
        ) or {}
        completed = {}
        checkpoint_was_normalized = False
        for key, value in saved.items():
            validate_reserved_mapping_privacy(
                value,
                internal_unit_ids=internal_work_unit_ids,
                surface="restored search checkpoint",
            )
            packet = normalize_search_packet(Beta8SearchPacket.model_validate(value))
            validate_reserved_mapping_privacy(
                packet,
                internal_unit_ids=internal_work_unit_ids,
                surface="normalized restored search checkpoint",
            )
            if packet.search_task_id != key:
                raise ValueError("search checkpoint key does not match search_task_id")
            completed[key] = packet
            checkpoint_was_normalized |= packet.model_dump(mode="json") != value
        if (
            search_executor.provider_id
            and any(
                task.search_task_key not in completed
                for task in orchestration.search_tasks
            )
        ):
            self._web_search_performed = True

        async def persist(packet):
            validate_reserved_mapping_privacy(
                packet,
                internal_unit_ids=internal_work_unit_ids,
                surface="search output",
            )
            completed[packet.search_task_id] = packet
            await self._save_stage(
                version.id, staged, "beta8_search_packets",
                {key: value.model_dump(mode="json") for key, value in completed.items()},
                transcript_fingerprint, self.composer.fixed_rules_hash(), worker_owner_id,
                upstream_hash=upstream_hash,
            )

        if checkpoint_was_normalized:
            await self._save_stage(
                version.id, staged, "beta8_search_packets",
                {key: value.model_dump(mode="json") for key, value in completed.items()},
                transcript_fingerprint, self.composer.fixed_rules_hash(), worker_owner_id,
                upstream_hash=upstream_hash,
            )

        packets = await search_executor.execute(
            orchestration.search_tasks, completed=completed, persist=persist
        )
        for packet in packets:
            validate_reserved_mapping_privacy(
                packet,
                internal_unit_ids=internal_work_unit_ids,
                surface="search output",
            )
        self._web_search_degraded_reason = self._search_degraded_reason(packets)
        return packets

    async def _run_revisions(
        self, *, version, staged, cards, orchestration, transcript, packets,
        transcript_fingerprint, upstream_hash, worker_owner_id,
        work_unit_ids_by_card_id=None,
    ) -> Sequence[Beta8FinalCard]:
        internal_work_unit_ids = {
            unit_id
            for unit_ids in (work_unit_ids_by_card_id or {}).values()
            for unit_id in unit_ids
        }
        cards_by_id = {card["card_id"]: card for card in cards}
        tasks_by_key = {task.revision_task_key: task for task in orchestration.revision_tasks}
        saved = self._stage_payload(
            staged, "beta8_revised_cards", transcript_fingerprint, upstream_hash
        ) or {}
        revised = dict(saved)
        packet_by_id = {packet.search_task_id: packet for packet in packets}
        final_cards: list[Beta8FinalCard] = []
        for decision in orchestration.card_decisions:
            if decision.operation == "drop":
                continue
            position = len(final_cards)
            final_id = f"final-card-{position + 1:04d}"
            if decision.operation == "keep":
                source = cards_by_id[decision.source_card_ids[0]]
                parsed = parse_beta8_card_markdown(source["markdown"])
                final_cards.append(Beta8FinalCard.model_validate({
                    "card_id": final_id, "scene_id": decision.target_scene_id,
                    "position": position, "title": parsed.title, "summary": parsed.summary,
                    "markdown": source["markdown"],
                    "source_segment_ids": source["source_segment_ids"],
                    "used_source_ids": [], "origin_card_ids": decision.source_card_ids,
                    "v1_markdown_sha256": source["v1_markdown_sha256"],
                    "final_markdown_sha256": source["v1_markdown_sha256"],
                    "untouched": True,
                    "work_communication_unit_ids": list(
                        (work_unit_ids_by_card_id or {}).get(source["card_id"], ())
                    ),
                }))
                continue
            task = tasks_by_key[decision.revision_task_key]
            result_payload = revised.get(task.revision_task_key)
            if result_payload is not None:
                validate_reserved_mapping_privacy(
                    result_payload,
                    internal_unit_ids=internal_work_unit_ids,
                    surface="restored revision checkpoint",
                )
                try:
                    cached_revision = Beta8RevisedCard.model_validate(result_payload)
                    if (
                        cached_revision.reserved_card_id != final_id
                        or cached_revision.operation != task.operation
                    ):
                        raise ValueError("cached revision identity does not match task")
                    cached_revision.validate_requirement_ids(
                        {item.requirement_id for item in task.requirements}
                    )
                    parse_beta8_card_markdown(cached_revision.markdown)
                except ValueError:
                    revised.pop(task.revision_task_key, None)
                    result_payload = None
            if result_payload is None:
                relevant_segments = [
                    item for item in transcript
                    if str(item["segment_id"]) in {
                        segment_id for requirement in task.requirements
                        for segment_id in requirement.evidence_segment_ids
                    }
                ]
                request = self.composer.compose_revision(
                    target_scene_id=task.target_scene_id,
                    source_cards=[
                        {
                            key: value
                            for key, value in cards_by_id[item].items()
                            if key != "work_communication_unit_ids"
                        }
                        for item in task.source_card_ids
                    ],
                    revision_task=task.model_dump(mode="json"),
                    transcript_segments=relevant_segments,
                    search_packets=[
                        packet.model_dump(mode="json")
                        for packet in search_packets_for_revision_task(
                            task, packet_by_id
                        )
                    ],
                )
                validate_model_request_privacy(
                    request,
                    internal_unit_ids=internal_work_unit_ids,
                    surface="revision request",
                )
                raw = await self._generate(
                    version, request, stage="revisions"
                )
                validate_reserved_mapping_privacy(
                    raw,
                    internal_unit_ids=internal_work_unit_ids,
                    surface="revision output",
                )

                def parse_and_validate_revision(value: str) -> Beta8RevisedCard:
                    parsed_revision = Beta8RevisedCard.model_validate(json.loads(value))
                    if (
                        parsed_revision.reserved_card_id != final_id
                        or parsed_revision.operation != task.operation
                    ):
                        raise ValueError(
                            "revision must return reserved_card_id="
                            f"{final_id} and operation={task.operation} unchanged"
                        )
                    parsed_revision.validate_requirement_ids(
                        {item.requirement_id for item in task.requirements}
                    )
                    parse_beta8_card_markdown(parsed_revision.markdown)
                    return parsed_revision

                for repair_index in range(3):
                    try:
                        revised_card = parse_and_validate_revision(raw)
                        break
                    except (ValueError, json.JSONDecodeError) as error:
                        if repair_index == 2:
                            raise
                        if isinstance(error, json.JSONDecodeError):
                            error_summary = "输出不是有效 JSON"
                        elif isinstance(error, ValidationError):
                            error_types = sorted({
                                str(item["type"])
                                for item in error.errors(include_input=False)
                            })
                            error_summary = (
                                "输出未通过 Beta8RevisedCard Schema："
                                + ", ".join(error_types)
                            )
                        else:
                            error_summary = (
                                "输出未通过修订身份、要求覆盖或 Markdown 结构校验"
                            )
                        repair = replace(
                            request,
                            instructions=(
                                request.instructions
                                + "\n\n上一次定向修订输出未通过运行时契约校验。"
                                "请仅基于原始 source_cards、revision_task、"
                                "transcript_segments 和 search_packets 重新生成；"
                                "不得改变修改任务的实质要求、删除证据或伪造来源。\n"
                                f"校验错误摘要：{error_summary}。"
                                f"必须返回 reserved_card_id={final_id} "
                                f"且 operation={task.operation}。"
                            ),
                        )
                        validate_model_request_privacy(
                            repair,
                            internal_unit_ids=internal_work_unit_ids,
                            surface="revision repair request",
                        )
                        raw = await self._generate(
                            version,
                            repair,
                            repair_attempted=True,
                            stage="revisions",
                            attempt_kind="schema_repair",
                        )
                        validate_reserved_mapping_privacy(
                            raw,
                            internal_unit_ids=internal_work_unit_ids,
                            surface="revision repair output",
                        )
                validate_reserved_mapping_privacy(
                    revised_card,
                    internal_unit_ids=internal_work_unit_ids,
                    surface="validated revision output",
                )
                result_payload = revised_card.model_dump(mode="json")
                revised[task.revision_task_key] = result_payload
                await self._save_stage(
                    version.id, staged, "beta8_revised_cards", revised,
                    transcript_fingerprint, self.composer.fixed_rules_hash(), worker_owner_id,
                    upstream_hash=upstream_hash,
                )
            revised_card = Beta8RevisedCard.model_validate(result_payload)
            validate_reserved_mapping_privacy(
                revised_card,
                internal_unit_ids=internal_work_unit_ids,
                surface="revision output",
            )
            revised_card.validate_requirement_ids({item.requirement_id for item in task.requirements})
            parsed = parse_beta8_card_markdown(revised_card.markdown)
            final_cards.append(Beta8FinalCard.model_validate({
                "card_id": final_id, "scene_id": task.target_scene_id,
                "position": position, "title": parsed.title, "summary": parsed.summary,
                "markdown": revised_card.markdown,
                "source_segment_ids": revised_card.source_segment_ids,
                "used_source_ids": revised_card.used_source_ids,
                "origin_card_ids": task.source_card_ids,
                "v1_markdown_sha256": (
                    cards_by_id[task.source_card_ids[0]]["v1_markdown_sha256"]
                    if len(task.source_card_ids) == 1 else None
                ),
                "final_markdown_sha256": sha256(revised_card.markdown.encode()).hexdigest(),
                "untouched": False,
                "work_communication_unit_ids": [
                    unit_id
                    for card_id in task.source_card_ids
                    for unit_id in (work_unit_ids_by_card_id or {}).get(card_id, ())
                ],
            }))
        return tuple(final_cards)

    @staticmethod
    def _requirements_for_unit(unit, requirements):
        if unit.scope == "global_report":
            return [item for item in requirements if not item.get("evidence_segment_ids")]
        unit_ids = {str(item["segment_id"]) for item in unit.transcript_segments}
        return [
            item for item in requirements
            if unit_ids & set(item.get("evidence_segment_ids", []))
        ]

    @staticmethod
    def _validate_final_requirement_checks(staged, requirements):
        expected = {item["requirement_id"] for item in requirements}
        if not expected:
            return
        record = Beta8StageRecord.model_validate(staged["beta8_final_audit_units"])
        completed: dict[str, list[bool]] = {key: [] for key in expected}
        for payload in record.payload.values():
            result = Beta8AuditResult.model_validate(payload)
            for check in result.revision_task_checks:
                if check.requirement_id in completed:
                    completed[check.requirement_id].append(check.completed)
        bad = sorted(key for key, values in completed.items() if not values or not all(values))
        if bad:
            raise ValueError(f"final audit did not complete requirements: {', '.join(bad)}")

    def _stage_payload(self, staged, key, transcript_fingerprint, upstream_hash):
        raw = staged.get(key)
        if raw is None:
            return None
        record = Beta8StageRecord.model_validate(raw)
        try:
            validate_stage_record(
                record, prompt_hash=self.composer.fixed_rules_hash(),
                transcript_fingerprint=transcript_fingerprint,
                provider_generation=self._provider_generation,
                upstream_artifact_hash=upstream_hash,
            )
        except CheckpointNotReusableError:
            staged.pop(key, None)
            return None
        if self._run_id:
            self._checkpoint_reused_stages.add(key)
        return record.payload

    async def _timed_stage(self, stage: str, operation):
        started = time.monotonic()
        try:
            return await operation
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1_000)
            self._stage_durations_ms[stage] = (
                self._stage_durations_ms.get(stage, 0) + elapsed_ms
            )

    async def _save_stage(
        self, version_id, staged, key, payload, transcript_fingerprint,
        prompt_hash, worker_owner_id, *, upstream_hash,
    ):
        async with self._checkpoint_lock:
            version = await self._version(version_id, worker_owner_id)
            record = Beta8StageRecord.create(
                payload=payload, prompt_hash=prompt_hash,
                transcript_fingerprint=transcript_fingerprint,
                provider_generation=version.credential_generation,
                upstream_artifact_hash=upstream_hash,
            )
            staged[key] = record.model_dump(mode="json")
            async with self.database.session() as session:
                statement = update(AnalysisVersion).where(
                    AnalysisVersion.id == version_id,
                    AnalysisVersion.status == "running",
                )
                if worker_owner_id is not None:
                    statement = statement.where(AnalysisVersion.worker_owner_id == worker_owner_id)
                saved = await session.execute(statement.values(
                    staged_results_json=json.dumps(staged, ensure_ascii=False),
                    pipeline_metrics_json=json.dumps(
                        self._metrics_payload(), ensure_ascii=False
                    ),
                ))
                if int(saved.rowcount) != 1:
                    await session.rollback()
                    raise LeaseLostError("Analysis worker lease was lost")
                await session.commit()

    async def _generate(
        self,
        version,
        request,
        *,
        allow_parallel=False,
        repair_attempted=False,
        stage: str | None = None,
        attempt_kind: str = "normal",
    ):
        invocation_id = str(uuid4())
        try:
            return await self.provider.generate(
                version.provider_id, system=request.instructions, user=request.user_data,
                model_id=version.model_id, scene_id=request.scene_id,
                max_tokens=request.max_tokens, timeout_seconds=request.timeout_seconds,
                segment_count=request.segment_count, allow_parallel=allow_parallel,
                repair_attempted=repair_attempted,
                thinking_enabled=request.thinking_enabled,
                diagnostic_invocation_id=invocation_id,
            )
        except ProviderAnalysisError as error:
            if error.partial_response:
                self._quarantine_response(
                    f"{request.scene_id}_truncated", error.partial_response
                )
            raise
        finally:
            metric_stage = stage or request.scene_id
            if metric_stage == "cross_card_orchestration":
                metric_stage = "editorial_review"
            elif metric_stage == "targeted_revision":
                metric_stage = "revisions"
            effective_kind = (
                "resume"
                if attempt_kind == "normal" and self._resumed_run
                else attempt_kind
            )
            diagnostics = sorted(
                [
                item
                for item in getattr(self.provider, "request_diagnostics", [])
                if getattr(item, "invocation_id", None) == invocation_id
                ],
                key=lambda item: int(getattr(item, "attempt_index", 0)),
            )
            for diagnostic in diagnostics:
                attempt_index = int(getattr(diagnostic, "attempt_index", 0))
                diagnostic_duration_ms = max(
                    0,
                    int(float(getattr(diagnostic, "elapsed_seconds", 0)) * 1_000),
                )
                self._append_model_metric(
                    invocation_id=invocation_id,
                    attempt_index=attempt_index,
                    stage=metric_stage,
                    attempt_kind=(
                        effective_kind if attempt_index == 0 else "retry"
                    ),
                    provider=str(
                        getattr(diagnostic, "provider_id", version.provider_id)
                    ),
                    model=str(getattr(diagnostic, "model_id", version.model_id)),
                    input_tokens=getattr(diagnostic, "input_tokens", None),
                    output_tokens=getattr(diagnostic, "output_tokens", None),
                    token_usage_unavailable_reason=getattr(
                        diagnostic, "token_usage_unavailable_reason", None
                    ),
                    duration_ms=diagnostic_duration_ms,
                    started_at=str(getattr(diagnostic, "started_at")),
                    finished_at=str(getattr(diagnostic, "finished_at")),
                )

    def _append_model_metric(
        self,
        *,
        invocation_id,
        attempt_index,
        stage,
        attempt_kind,
        provider,
        model,
        input_tokens,
        output_tokens,
        token_usage_unavailable_reason,
        duration_ms,
        started_at,
        finished_at,
    ) -> None:
        metric = ModelCallMetric(
            run_id=self._run_id,
            invocation_id=invocation_id,
            attempt_index=attempt_index,
            stage=stage,
            attempt_kind=attempt_kind,
            provider=provider,
            model=model,
            started_at=started_at,
            finished_at=finished_at,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_usage_unavailable_reason=token_usage_unavailable_reason,
            duration_ms=duration_ms,
            cost=None,
            cost_unavailable_reason="provider did not report cost",
            checkpoint_reused=False,
        )
        self._model_call_metrics.append(metric)
        self._model_calls += 1
        self._model_duration_ms += duration_ms
        self._run_model_duration_ms += duration_ms
        if input_tokens is not None:
            self._input_tokens += int(input_tokens)
            self._run_input_tokens += int(input_tokens)
        else:
            self._input_tokens_known = False
            self._run_input_tokens_known = False
        if output_tokens is not None:
            self._output_tokens += int(output_tokens)
            self._run_output_tokens += int(output_tokens)
        else:
            self._output_tokens_known = False
            self._run_output_tokens_known = False
        if token_usage_unavailable_reason:
            self._token_usage_unavailable_reasons.add(
                str(token_usage_unavailable_reason)
            )

    async def _save_metrics(self, version_id, packets, worker_owner_id):
        search_called = (
            self._web_search_performed
            if packets is None
            else bool(packets and self._run_search_provider_id)
        )
        degraded = (
            self._web_search_degraded_reason
            if packets is None
            else self._search_degraded_reason(packets)
        )
        metrics = self._metrics_payload(
            web_search_performed=search_called,
            web_search_degraded_reason=degraded,
        )
        if self._run_finished_at is None:
            self._run_finished_at = datetime.now(timezone.utc).isoformat()
            metrics = self._metrics_payload(
                web_search_performed=search_called,
                web_search_degraded_reason=degraded,
            )
        async with self.database.session() as session:
            statement = update(AnalysisVersion).where(
                AnalysisVersion.id == version_id, AnalysisVersion.status == "running"
            )
            if worker_owner_id is not None:
                statement = statement.where(AnalysisVersion.worker_owner_id == worker_owner_id)
            result = await session.execute(statement.values(
                pipeline_metrics_json=json.dumps(metrics, ensure_ascii=False)
            ))
            if int(result.rowcount) != 1:
                await session.rollback()
                raise LeaseLostError("Analysis worker lease was lost")
            await session.commit()

    def _metrics_payload(
        self,
        *,
        web_search_performed: bool | None = None,
        web_search_degraded_reason: str | None = None,
    ) -> dict[str, object]:
        current_search_usage = self._native_search_usage_snapshot()
        run_search_input_unknown = (
            current_search_usage["input_unavailable_count"]
            > self._search_usage_offset["input_unavailable_count"]
        )
        run_search_output_unknown = (
            current_search_usage["output_unavailable_count"]
            > self._search_usage_offset["output_unavailable_count"]
        )
        run_search_input_tokens = (
            None
            if run_search_input_unknown
            else max(
                0,
                current_search_usage["known_input_tokens"]
                - self._search_usage_offset["known_input_tokens"],
            )
        )
        run_search_output_tokens = (
            None
            if run_search_output_unknown
            else max(
                0,
                current_search_usage["known_output_tokens"]
                - self._search_usage_offset["known_output_tokens"],
            )
        )
        run_search_responses = max(
            0,
            current_search_usage["response_count"]
            - self._search_usage_offset["response_count"],
        )
        run_search_tool_calls = max(
            0,
            current_search_usage["tool_call_count"]
            - self._search_usage_offset["tool_call_count"],
        )
        search_input_tokens = (
            None
            if self._search_usage_base["input_tokens"] is None
            or run_search_input_tokens is None
            else self._search_usage_base["input_tokens"] + run_search_input_tokens
        )
        search_output_tokens = (
            None
            if self._search_usage_base["output_tokens"] is None
            or run_search_output_tokens is None
            else self._search_usage_base["output_tokens"] + run_search_output_tokens
        )
        search_token_reasons = set(self._search_token_usage_unavailable_reasons)
        current_search_reason = current_search_usage.get(
            "token_usage_unavailable_reason"
        )
        if (
            run_search_input_unknown or run_search_output_unknown
        ) and current_search_reason:
            search_token_reasons.add(str(current_search_reason))
        run_duration_ms = max(
            0, int((time.monotonic() - self._run_started_monotonic) * 1_000)
        )
        run_calls = [
            item for item in self._model_call_metrics if item.run_id == self._run_id
        ]
        payload = PipelineMetrics(
            local_duration_ms=0,
            model_duration_ms=self._model_duration_ms,
            total_duration_ms=self._historical_total_duration_ms + run_duration_ms,
            input_tokens=(self._input_tokens if self._input_tokens_known else None),
            output_tokens=(self._output_tokens if self._output_tokens_known else None),
            model_call_count=self._model_calls,
            search_input_tokens=search_input_tokens,
            search_output_tokens=search_output_tokens,
            search_token_usage_unavailable_reason=(
                "; ".join(sorted(search_token_reasons)) or None
            ),
            search_model_response_count=(
                self._search_usage_base["response_count"] + run_search_responses
            ),
            web_search_tool_call_count=(
                self._search_usage_base["tool_call_count"] + run_search_tool_calls
            ),
            web_search_performed=(
                self._web_search_performed
                if web_search_performed is None else web_search_performed
            ),
            web_search_degraded_reason=(
                self._web_search_degraded_reason
                if web_search_degraded_reason is None
                else web_search_degraded_reason
            ),
            run_id=self._run_id,
            run_started_at=self._run_started_at,
            run_finished_at=self._run_finished_at,
            run_duration_ms=run_duration_ms,
            run_model_duration_ms=self._run_model_duration_ms,
            run_input_tokens=(
                self._run_input_tokens if self._run_input_tokens_known else None
            ),
            run_output_tokens=(
                self._run_output_tokens if self._run_output_tokens_known else None
            ),
            token_usage_unavailable_reason=(
                "; ".join(sorted(self._token_usage_unavailable_reasons)) or None
            ),
            new_model_call_count=len(run_calls),
            historical_model_call_count=self._historical_model_call_count,
            run_search_input_tokens=run_search_input_tokens,
            run_search_output_tokens=run_search_output_tokens,
            run_search_model_response_count=run_search_responses,
            run_web_search_tool_call_count=run_search_tool_calls,
            stage_durations_ms=self._stage_durations_ms,
            checkpoint_reused_stages=tuple(sorted(self._checkpoint_reused_stages)),
            final_audit_score=self._final_audit_score,
            cost=None,
            cost_unavailable_reason=(
                "provider did not report cost" if run_calls else "no model calls in this run"
            ),
            model_calls=tuple(self._model_call_metrics),
        )
        return payload.model_dump(mode="json")

    def _native_search_usage_snapshot(self) -> dict[str, object]:
        snapshot = getattr(self.provider, "native_search_usage_snapshot", None)
        if callable(snapshot):
            return dict(snapshot())
        totals = getattr(self.provider, "native_search_usage_totals", {})
        input_tokens = totals.get("input_tokens", 0)
        output_tokens = totals.get("output_tokens", 0)
        return {
            "known_input_tokens": int(input_tokens or 0),
            "known_output_tokens": int(output_tokens or 0),
            "input_unavailable_count": int(input_tokens is None),
            "output_unavailable_count": int(output_tokens is None),
            "token_usage_unavailable_reason": totals.get(
                "token_usage_unavailable_reason"
            ),
            "response_count": int(totals.get("response_count", 0) or 0),
            "tool_call_count": int(totals.get("tool_call_count", 0) or 0),
        }

    @staticmethod
    def _search_degraded_reason(packets):
        failed = [packet.error_summary for packet in packets if packet.status != "success"]
        return "; ".join(item for item in failed if item) or None

    @staticmethod
    def _load_staged(raw):
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Beta 8 staged results are invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Beta 8 staged results must be an object")
        return value

    async def _version(self, version_id, worker_owner_id):
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        if version is None:
            raise LookupError(f"Unknown analysis version: {version_id}")
        if version.status != "running":
            raise ValueError(f"Analysis version is not running: {version.status}")
        if worker_owner_id is not None and version.worker_owner_id != worker_owner_id:
            raise LeaseLostError("Analysis worker lease was lost")
        return version

    async def _transcript(self, job_id):
        async with self.database.session() as session:
            rows = list((await session.execute(
                select(Transcript, JobFile)
                .join(JobFile, JobFile.id == Transcript.job_file_id)
                .where(
                    JobFile.job_id == job_id,
                    Transcript.risk_classified.is_(True),
                    Transcript.is_reliable.is_(True),
                )
                .order_by(JobFile.position, Transcript.segment_index)
            )).all())
        return [{
            "segment_id": f"seg_{file.position}_{row.segment_index}",
            "file_id": file.id, "file_name": file.original_name,
            "recording_started_at": file.recording_started_at,
            "timezone": file.timezone, "start_ms": row.start_ms, "end_ms": row.end_ms,
            "speaker_id": row.speaker_id or "unknown", "text": row.text,
        } for row, file in rows]
