from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Sequence

from audio_memory.prompts.beta8_pipeline_schema import (
    Beta8AuditResult,
    Beta8OrchestrationResult,
    Beta8RevisedCard,
    Beta8SearchPacket,
)
from audio_memory.prompts.beta8_event_index_schema import (
    Beta8EventIndexDraft,
    Beta8NormalizedEventIndex,
)
from audio_memory.prompts.beta8_scene_schema import (
    BETA8_SCENE_IDS,
    Beta8SceneResult,
    Beta8UnifiedV1Result,
)


SCENE_IDS = BETA8_SCENE_IDS
_PROMPT_ROOT = Path(__file__).with_name("beta8")
_SHARED_REPORT_QUALITY_PATH = _PROMPT_ROOT / "report-quality.md"
_EVENT_INDEX_PATH = _PROMPT_ROOT / "event-index.md"
_ALL_SCENES_V1_PATH = _PROMPT_ROOT / "all-scenes-v1.md"
_SCHEMA_SOURCE_PATHS = {
    "event_index": Path(__file__).with_name("beta8_event_index_schema.py"),
    "scene": Path(__file__).with_name("beta8_scene_schema.py"),
    "pipeline": Path(__file__).with_name("beta8_pipeline_schema.py"),
}
_TWO_LAYER_INDEX_NORMALIZER_POLICY = "beta8_two_layer_index_normalizer_v3_explicit_ranges"
_SCENE_FILENAMES = {
    "work_communication": "work-communication.md",
    "parenting_family": "parenting-family.md",
    "health_state": "health-state.md",
    "content_consumption": "content-consumption.md",
    "inspiration_insight": "inspiration-insight.md",
    "self_growth": "self-growth.md",
    "life_decisions": "life-decisions.md",
}
_DOWNSTREAM_FILENAMES = {
    "unified_audit": "unified-audit.md",
    "cross_card_orchestration": "cross-card-orchestration.md",
    "search_execution": "search-execution.md",
    "targeted_revision": "targeted-revision.md",
}
_MARKDOWN_CONTRACT = (
    "\n\n运行时 Markdown 最小格式契约：每张卡片的第一个非空行必须是 `# <标题>`；"
    "标题之后、下一个 Markdown 标题之前必须包含非空核心摘要。除这一格式要求外，"
    "不得重组或压缩完整 Markdown。"
)
_SCHEMA_CONTRACT = (
    "\n\n运行时 JSON 契约：只返回一个与下列 JSON Schema 完全匹配的 JSON 对象。"
    "字段名、嵌套层级和取值类型必须严格一致；不得使用 Prompt 示例中的历史别名，"
    "不得增加 Schema 之外的字段。\n"
)
_SHARED_QUALITY_PLACEHOLDER = "{{BETA8_SHARED_QUALITY_CONTRACT}}"
_SCENE_CAPABILITIES_PLACEHOLDER = "{{BETA8_SEVEN_SCENE_CAPABILITIES}}"
_DEFAULT_REQUEST_POLICIES = {
    "event_index": {
        "max_tokens": 32_000,
        "timeout_seconds": 300.0,
        "thinking_enabled": True,
    },
    "all_scenes_v1": {
        "max_tokens": 64_000,
        "timeout_seconds": 600.0,
        "thinking_enabled": True,
    },
}


@dataclass(frozen=True, slots=True)
class Beta8ModelRequest:
    scene_id: str
    instructions: str
    user_data: str
    schema_json: str
    max_tokens: int
    timeout_seconds: float
    segment_count: int
    thinking_enabled: bool | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _untrusted(tag: str, value: Any) -> str:
    body = _canonical_json(value).replace("</", "<\\/")
    return f'<{tag} untrusted="true">{body}</{tag}>'


def _with_schema(prompt: str, schema_json: str, *, markdown: bool = False) -> str:
    return (
        prompt
        + (_MARKDOWN_CONTRACT if markdown else "")
        + _SCHEMA_CONTRACT
        + schema_json
    )


class Beta8PromptComposer:
    """Compose strict requests from packaged prompts and generated schemas."""

    @staticmethod
    def _shared_report_quality() -> str:
        return _SHARED_REPORT_QUALITY_PATH.read_text()

    def _with_shared_report_quality(self, prompt: str) -> str:
        return (
            prompt
            + "\n\n## 必须执行的共享报告质量契约\n\n"
            + self._shared_report_quality()
        )

    def _read(self, prompt_id: str) -> str:
        if prompt_id == "event_index":
            return _EVENT_INDEX_PATH.read_text()
        if prompt_id == "all_scenes_v1":
            return _ALL_SCENES_V1_PATH.read_text()
        if prompt_id in _SCENE_FILENAMES:
            return (_PROMPT_ROOT / "scenes" / _SCENE_FILENAMES[prompt_id]).read_text()
        return (_PROMPT_ROOT / _DOWNSTREAM_FILENAMES[prompt_id]).read_text()

    def compose_event_index(
        self,
        *,
        transcript_markdown: str,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Beta8ModelRequest:
        policy = _DEFAULT_REQUEST_POLICIES["event_index"]
        schema_json = _canonical_json(Beta8EventIndexDraft.model_json_schema())
        return Beta8ModelRequest(
            scene_id="event_index",
            instructions=_with_schema(self._read("event_index"), schema_json),
            user_data=_untrusted(
                "beta8_event_index_data",
                {"transcript_markdown": transcript_markdown},
            ),
            schema_json=schema_json,
            max_tokens=int(
                policy["max_tokens"] if max_tokens is None else max_tokens
            ),
            timeout_seconds=float(
                policy["timeout_seconds"]
                if timeout_seconds is None
                else timeout_seconds
            ),
            segment_count=transcript_markdown.count("<segment "),
            thinking_enabled=bool(policy["thinking_enabled"]),
        )

    def compose_all_scenes_v1(
        self,
        *,
        transcript_markdown: str,
        event_index: Beta8NormalizedEventIndex,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Beta8ModelRequest:
        policy = _DEFAULT_REQUEST_POLICIES["all_scenes_v1"]
        scene_capabilities = "\n\n".join(
            (
                f"## 场景完整能力：{scene_id}\n\n"
                f"{self._read(scene_id)}"
            )
            for scene_id in SCENE_IDS
        )
        prompt = self._read("all_scenes_v1")
        prompt = prompt.replace(
            _SHARED_QUALITY_PLACEHOLDER,
            self._shared_report_quality(),
        ).replace(
            _SCENE_CAPABILITIES_PLACEHOLDER,
            scene_capabilities,
        )
        if _SHARED_QUALITY_PLACEHOLDER in prompt or _SCENE_CAPABILITIES_PLACEHOLDER in prompt:
            raise ValueError("all-scenes V1 prompt template was not fully composed")
        schema_json = _canonical_json(Beta8UnifiedV1Result.model_json_schema())
        return Beta8ModelRequest(
            scene_id="all_scenes_v1",
            instructions=_with_schema(prompt, schema_json, markdown=True),
            user_data=_untrusted(
                "beta8_all_scenes_v1_data",
                {
                    "transcript_markdown": transcript_markdown,
                    "event_index": event_index.model_dump(mode="json"),
                },
            ),
            schema_json=schema_json,
            max_tokens=int(
                policy["max_tokens"] if max_tokens is None else max_tokens
            ),
            timeout_seconds=float(
                policy["timeout_seconds"]
                if timeout_seconds is None
                else timeout_seconds
            ),
            segment_count=transcript_markdown.count("<segment "),
            thinking_enabled=bool(policy["thinking_enabled"]),
        )

    def compose_scene(
        self,
        scene_id: str,
        *,
        transcript_markdown: str,
        max_tokens: int = 32_000,
        timeout_seconds: float = 300.0,
    ) -> Beta8ModelRequest:
        if scene_id not in SCENE_IDS:
            raise ValueError(f"Unknown Beta 8 scene: {scene_id}")
        safe_transcript = transcript_markdown.replace(
            "</beta8_scene_data>", "<\\/beta8_scene_data>"
        )
        schema_json = _canonical_json(Beta8SceneResult.model_json_schema())
        return Beta8ModelRequest(
            scene_id=scene_id,
            instructions=_with_schema(self._read(scene_id), schema_json, markdown=True),
            user_data=(
                '<beta8_scene_data untrusted="true">'
                f"{safe_transcript}</beta8_scene_data>"
            ),
            schema_json=schema_json,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            segment_count=transcript_markdown.count("<segment "),
        )

    def compose_audit(
        self,
        *,
        phase: str,
        scope: str,
        audit_unit_id: str,
        cards: Sequence[dict[str, Any]],
        transcript_segments: Sequence[dict[str, Any]],
        revision_requirements: Sequence[dict[str, Any]],
        max_tokens: int = 32_000,
        timeout_seconds: float = 300.0,
    ) -> Beta8ModelRequest:
        projected_cards = [
            {key: value for key, value in card.items() if key != "work_communication_unit_ids"}
            for card in cards
        ]
        data = {
            "audit_phase": phase,
            "audit_scope": scope,
            "audit_unit_id": audit_unit_id,
            "cards": projected_cards,
            "transcript_segments": list(transcript_segments),
            "revision_requirements": list(revision_requirements),
        }
        schema_json = _canonical_json(Beta8AuditResult.model_json_schema())
        return Beta8ModelRequest(
            scene_id="unified_audit",
            instructions=_with_schema(
                self._with_shared_report_quality(self._read("unified_audit")),
                schema_json,
            ),
            user_data=_untrusted("beta8_audit_data", data),
            schema_json=schema_json,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            segment_count=len(transcript_segments),
        )

    def compose_orchestration(
        self,
        *,
        cards: Sequence[dict[str, Any]],
        audit: dict[str, Any],
        search_candidates: Sequence[dict[str, Any]],
        todo_candidates: Sequence[dict[str, Any]],
        max_tokens: int = 32_000,
        timeout_seconds: float = 300.0,
    ) -> Beta8ModelRequest:
        projected_cards = [
            {key: value for key, value in card.items() if key != "work_communication_unit_ids"}
            for card in cards
        ]
        data = {
            "cards": projected_cards, "audit": audit,
            "search_candidates": list(search_candidates),
            "todo_candidates": list(todo_candidates),
        }
        schema_json = _canonical_json(Beta8OrchestrationResult.model_json_schema())
        return Beta8ModelRequest(
            scene_id="cross_card_orchestration",
            instructions=_with_schema(
                self._with_shared_report_quality(
                    self._read("cross_card_orchestration")
                ),
                schema_json,
            ),
            user_data=_untrusted("beta8_orchestration_data", data),
            schema_json=schema_json,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            segment_count=0,
        )

    def compose_search(
        self, *, task: dict[str, Any], max_tokens: int = 6_000,
        timeout_seconds: float = 180.0,
    ) -> Beta8ModelRequest:
        schema_json = _canonical_json(Beta8SearchPacket.model_json_schema())
        return Beta8ModelRequest(
            scene_id="search_execution",
            instructions=_with_schema(self._read("search_execution"), schema_json),
            user_data=_untrusted("beta8_search_data", task),
            schema_json=schema_json,
            max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            segment_count=len(task.get("related_segment_ids", [])),
        )

    def compose_revision(
        self,
        *,
        target_scene_id: str,
        source_cards: Sequence[dict[str, Any]],
        revision_task: dict[str, Any],
        transcript_segments: Sequence[dict[str, Any]],
        search_packets: Sequence[dict[str, Any]],
        max_tokens: int = 32_000,
        timeout_seconds: float = 300.0,
    ) -> Beta8ModelRequest:
        data = {
            "source_cards": list(source_cards), "revision_task": revision_task,
            "transcript_segments": list(transcript_segments),
            "search_packets": list(search_packets),
        }
        schema_json = _canonical_json(Beta8RevisedCard.model_json_schema())
        revision_instructions = (
            self._with_shared_report_quality(self._read("targeted_revision"))
            + f"\n\n## 目标场景完整能力：{target_scene_id}\n\n"
            + self._read(target_scene_id)
        )
        return Beta8ModelRequest(
            scene_id="targeted_revision",
            instructions=_with_schema(
                revision_instructions,
                schema_json, markdown=True,
            ),
            user_data=_untrusted("beta8_revision_data", data),
            schema_json=schema_json,
            max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            segment_count=len(transcript_segments),
        )

    @classmethod
    def prompt_manifest(cls) -> Sequence[dict[str, str]]:
        paths = [
            ("event_index", _EVENT_INDEX_PATH),
            ("all_scenes_v1", _ALL_SCENES_V1_PATH),
            *( (scene_id, _PROMPT_ROOT / "scenes" / _SCENE_FILENAMES[scene_id]) for scene_id in SCENE_IDS ),
            ("shared_report_quality", _SHARED_REPORT_QUALITY_PATH),
            *( (prompt_id, _PROMPT_ROOT / filename) for prompt_id, filename in _DOWNSTREAM_FILENAMES.items() ),
        ]
        return tuple({
            "prompt_id": prompt_id,
            "path": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        } for prompt_id, path in paths)

    @classmethod
    def schema_manifest(cls) -> Sequence[dict[str, str]]:
        return tuple({
            "schema_id": schema_id,
            "path": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        } for schema_id, path in _SCHEMA_SOURCE_PATHS.items())

    @classmethod
    def fixed_rules_hash(cls) -> str:
        canonical = _canonical_json({
            "prompts": [
                {"prompt_id": item["prompt_id"], "sha256": item["sha256"]}
                for item in cls.prompt_manifest()
            ],
            "schema_sources": [
                {"schema_id": item["schema_id"], "sha256": item["sha256"]}
                for item in cls.schema_manifest()
            ],
            "markdown_contract": _MARKDOWN_CONTRACT,
            "schema_contract": _SCHEMA_CONTRACT,
            "request_policies": _DEFAULT_REQUEST_POLICIES,
            "event_index_normalizer_policy": _TWO_LAYER_INDEX_NORMALIZER_POLICY,
            "schemas": {
                "event_index_draft": Beta8EventIndexDraft.model_json_schema(),
                "normalized_event_index": Beta8NormalizedEventIndex.model_json_schema(),
                "unified_v1": Beta8UnifiedV1Result.model_json_schema(),
                "scene": Beta8SceneResult.model_json_schema(),
                "audit": Beta8AuditResult.model_json_schema(),
                "orchestration": Beta8OrchestrationResult.model_json_schema(),
                "search": Beta8SearchPacket.model_json_schema(),
                "revision": Beta8RevisedCard.model_json_schema(),
            },
        })
        return sha256(canonical.encode()).hexdigest()

    @classmethod
    def approved_source_mappings(cls, repository_root: Path) -> Sequence[tuple[Path, Path]]:
        docs = repository_root / "docs" / "beta8"
        mappings: list[tuple[Path, Path]] = [
            (_EVENT_INDEX_PATH, docs / "pipeline-prompts-v2" / "01-event-index-prompt.md"),
            (_ALL_SCENES_V1_PATH, docs / "pipeline-prompts-v2" / "02-all-scenes-v1-prompt.md"),
            (_PROMPT_ROOT / "scenes" / "work-communication.md", docs / "work-prompt-composition-review-v3-1" / "03-work-composed-v1-generation-prompt.md"),
        ]
        for scene_id in SCENE_IDS[1:]:
            mappings.append((
                _PROMPT_ROOT / "scenes" / _SCENE_FILENAMES[scene_id],
                docs / "seven-scene-golden-prompts" / _SCENE_FILENAMES[scene_id],
            ))
        downstream = {
            "unified-audit.md": "01-unified-audit-prompt.md",
            "cross-card-orchestration.md": "02-cross-card-orchestration-prompt.md",
            "search-execution.md": "03-search-execution-protocol.md",
            "targeted-revision.md": "04-targeted-revision-prompt.md",
        }
        mappings.extend(
            (_PROMPT_ROOT / packaged, docs / "pipeline-prompts-v1" / approved)
            for packaged, approved in downstream.items()
        )
        mappings.append((
            _SHARED_REPORT_QUALITY_PATH,
            docs / "pipeline-prompts-v2" / "00-shared-report-quality-contract.md",
        ))
        return tuple(mappings)
