from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from hashlib import sha256
from importlib.resources import files

from audio_memory.analysis import dossiers as dossier_policy
from audio_memory.analysis import windows as analysis_windows
from audio_memory.analysis.clusters import TranscriptCluster
from audio_memory.analysis.dossiers import SceneDossier, dossiers_for_scene
from audio_memory.prompts import evidence as evidence_policy
from audio_memory.prompts.event_schema import EventMap
from audio_memory.prompts.store import PROMPT_SCENES, PromptDocument


@dataclass(frozen=True, slots=True)
class ModelRequestPolicy:
    max_tokens: int
    timeout_seconds: float


MODEL_REQUEST_POLICIES = {
    "event-map": ModelRequestPolicy(max_tokens=32_768, timeout_seconds=180),
    "director": ModelRequestPolicy(max_tokens=16_384, timeout_seconds=120),
    "scene": ModelRequestPolicy(max_tokens=16_384, timeout_seconds=120),
    "profile": ModelRequestPolicy(max_tokens=8_192, timeout_seconds=120),
}


@dataclass(frozen=True, slots=True)
class ModelRequest:
    scene_id: str
    prompt_version: int
    schema_version: int
    system_rules: str
    scene_prompt: str
    user_data: str
    schema_json: str
    max_tokens: int
    timeout_seconds: float
    segment_count: int
    common_rules: str = ""

    @property
    def rendered_instructions(self) -> str:
        return (
            "<layer_1_system_security>\n"
            f"{self.system_rules}\n"
            "</layer_1_system_security>\n\n"
            "<layer_2_fixed_analysis_rules>\n"
            f"{self.common_rules}\n"
            "</layer_2_fixed_analysis_rules>\n\n"
            "<layer_3_user_editable_scene_prompt>\n"
            f"{escape(self.scene_prompt)}\n"
            "</layer_3_user_editable_scene_prompt>\n\n"
            "<layer_4_json_schema>\n"
            f"{self.schema_json}\n"
            "</layer_4_json_schema>"
        )


class PromptComposer:
    SCHEMA_VERSION = 3

    @classmethod
    def fixed_rules_hash(cls) -> str:
        payload = {
            "prompts": {
                name: cls._fixed_prompt(name)
                for name in (
                    "system.md",
                    "event-map.md",
                    "director.md",
                    "common-scene.md",
                )
            },
            "schema_version": cls.SCHEMA_VERSION,
            "cluster_policy": {
                "gap_ms": analysis_windows.ANALYSIS_WINDOW_GAP_MS,
                "max_span_ms": analysis_windows.ANALYSIS_WINDOW_MAX_SPAN_MS,
                "max_segments": analysis_windows.ANALYSIS_WINDOW_MAX_SEGMENTS,
            },
            "event_map_policy": {
                "event_map_semantic_repair_attempts": (
                    analysis_windows.EVENT_MAP_SEMANTIC_REPAIR_ATTEMPTS
                ),
            },
            "scene_policy": {
                "evidence_policy_version": evidence_policy.EVIDENCE_POLICY_VERSION,
                "scene_semantic_repair_attempts": (
                    evidence_policy.SCENE_SEMANTIC_REPAIR_ATTEMPTS
                ),
            },
            "dossier_policy": {
                "max_span_ms": dossier_policy.DOSSIER_MAX_SPAN_MS,
                "max_segments": dossier_policy.DOSSIER_MAX_SEGMENTS,
                "adjacent_clusters_per_side": 1,
            },
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def compose_event_map(
        self,
        *,
        transcript: list[dict[str, object]],
        profile: list[dict[str, object]],
        schema: dict[str, object],
        window_id: str | None = None,
        semantic_retry: bool = False,
    ) -> ModelRequest:
        policy = MODEL_REQUEST_POLICIES["event-map"]
        common_rules = self._fixed_prompt("event-map.md")
        if semantic_retry:
            allowed_ids = [str(item["segment_id"]) for item in transcript]
            common_rules += (
                "\n\n服务端校验反馈（必须修正）：上一轮输出引用了当前窗口之外的证据 ID。"
                "本轮只能逐字使用以下 allowed_segment_ids 中的值；不要构造、续写或猜测 ID。"
                "如果某个事件没有合法直接证据，就不要输出该事件。\n"
                f"allowed_segment_ids={json.dumps(allowed_ids, ensure_ascii=False)}"
            )
        return ModelRequest(
            scene_id=(f"event-map:{window_id}" if window_id else "event-map"),
            prompt_version=0,
            schema_version=self.SCHEMA_VERSION,
            system_rules=self._fixed_prompt("system.md"),
            common_rules=common_rules,
            scene_prompt="",
            user_data="\n".join(
                [
                    self._untrusted_packet(
                        "transcript_data", self._event_map_transcript(transcript)
                    ),
                    self._untrusted_packet("profile_data", profile),
                ]
            ),
            schema_json=self._schema_json(schema),
            max_tokens=policy.max_tokens,
            timeout_seconds=policy.timeout_seconds,
            segment_count=len(transcript),
        )

    def compose_director(
        self,
        *,
        cluster: TranscriptCluster,
        event_hints: list[dict[str, object]],
        schema: dict[str, object],
    ) -> ModelRequest:
        policy = MODEL_REQUEST_POLICIES["director"]
        return ModelRequest(
            scene_id=f"director:{cluster.cluster_id}",
            prompt_version=0,
            schema_version=self.SCHEMA_VERSION,
            system_rules=self._fixed_prompt("system.md"),
            common_rules=self._fixed_prompt("director.md"),
            scene_prompt="",
            user_data="\n".join(
                [
                    self._untrusted_packet(
                        "transcript_clusters", [self._director_cluster(cluster)]
                    ),
                    self._untrusted_packet("event_hints", event_hints),
                ]
            ),
            schema_json=self._schema_json(schema),
            max_tokens=policy.max_tokens,
            timeout_seconds=policy.timeout_seconds,
            segment_count=len(cluster.segments),
        )

    def compose_scene(
        self,
        scene_id: str,
        *,
        transcript: list[dict[str, object]],
        event_map: EventMap,
        dossiers: list[SceneDossier] | None = None,
        profile: list[dict[str, object]],
        prompt: PromptDocument,
        schema: dict[str, object],
        semantic_retry: bool = False,
    ) -> ModelRequest:
        if scene_id not in PROMPT_SCENES or prompt.scene_id != scene_id:
            raise ValueError("Prompt scene does not match request scene")
        policy = MODEL_REQUEST_POLICIES["scene"]
        if dossiers is None:
            scene_transcript = self._scene_transcript(transcript, event_map)
            event_packet = event_map.model_dump(mode="json")
            assigned_segment_count = sum(
                len(event["segments"]) for event in scene_transcript["events"]
            )
        else:
            routed_dossiers = dossiers_for_scene(dossiers, scene_id)
            if not routed_dossiers:
                raise ValueError("scene request requires at least one routed dossier")
            scene_transcript = self._scene_dossiers(transcript, routed_dossiers)
            event_packet = self._event_map_without_compatibility(event_map)
            assigned_segment_count = len(
                {
                    segment_id
                    for dossier in routed_dossiers
                    for segment_id in dossier.allowed_segment_ids
                }
            )
        common_rules = self._fixed_prompt("common-scene.md")
        if semantic_retry:
            common_rules += (
                "\n\n服务端校验反馈（必须修正）：上一轮输出未通过场景档案证据校验。"
                "所有事件 ID 必须来自对应 dossier 的 primary_event_id 或 source_event_ids；"
                "所有 evidence_segment_ids 必须逐字来自同一 dossier 的 allowed_segment_ids，"
                "且文件和时间必须落在该 dossier 内。身份不可靠时不得生成 user/shared 待办、"
                "用户行为评价或强归因；删除证据不足的内容，不要猜测或构造 ID。"
            )
        return ModelRequest(
            scene_id=scene_id,
            prompt_version=prompt.version,
            schema_version=self.SCHEMA_VERSION,
            system_rules=self._fixed_prompt("system.md"),
            common_rules=common_rules,
            scene_prompt=prompt.content,
            user_data="\n".join(
                [
                    self._untrusted_packet("transcript_data", scene_transcript),
                    self._untrusted_packet(
                        "event_map", event_packet
                    ),
                    self._untrusted_packet("profile_data", profile),
                ]
            ),
            schema_json=self._schema_json(schema),
            max_tokens=policy.max_tokens,
            timeout_seconds=policy.timeout_seconds,
            segment_count=assigned_segment_count,
        )

    @staticmethod
    def _fixed_prompt(name: str) -> str:
        return files("audio_memory.prompts").joinpath(name).read_text().strip()

    @staticmethod
    def _schema_json(schema: dict[str, object]) -> str:
        return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _event_map_transcript(
        transcript: list[dict[str, object]],
    ) -> dict[str, list[dict[str, object]]]:
        files_by_id: dict[str, dict[str, object]] = {}
        segments: list[dict[str, object]] = []
        for item in transcript:
            file_id = str(item["file_id"])
            if file_id not in files_by_id:
                files_by_id[file_id] = {
                    "id": file_id,
                    "name": item["file_name"],
                    "recording_started_at": item.get("recording_started_at"),
                    "local_date": item.get("local_date"),
                    "timezone": item.get("timezone"),
                }
            segments.append(
                {
                    "id": str(item["segment_id"]),
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "text": item["text"],
                }
            )
        return {"files": list(files_by_id.values()), "segments": segments}

    @staticmethod
    def _director_cluster(cluster: TranscriptCluster) -> dict[str, object]:
        return {
            "cluster_id": cluster.cluster_id,
            "file_id": cluster.file_id,
            "file_name": cluster.file_name,
            "start_ms": cluster.start_ms,
            "end_ms": cluster.end_ms,
            "segments": [
                {
                    "segment_id": str(item["segment_id"]),
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "speaker_id": item["speaker_id"],
                    "text": item["text"],
                }
                for item in cluster.segments
            ],
        }

    @staticmethod
    def _scene_transcript(
        transcript: list[dict[str, object]],
        event_map: EventMap,
    ) -> dict[str, list[dict[str, object]]]:
        segments = {
            str(item["segment_id"]): item
            for item in transcript
        }
        events: list[dict[str, object]] = []
        for event in event_map.events:
            projected_segments: list[dict[str, object]] = []
            for segment_id in event.evidence_segment_ids:
                item = segments.get(segment_id)
                if item is None:
                    raise ValueError("Event map references unavailable transcript evidence")
                projected_segments.append(
                    {
                        "id": segment_id,
                        "start_ms": item["start_ms"],
                        "end_ms": item["end_ms"],
                        "speaker_id": item["speaker_id"],
                        "text": item["text"],
                    }
                )
            events.append(
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "title": event.title,
                    "segments": projected_segments,
                }
            )
        return {"events": events}

    @staticmethod
    def _scene_dossiers(
        transcript: list[dict[str, object]],
        dossiers: list[SceneDossier],
    ) -> dict[str, list[dict[str, object]]]:
        segments = {str(item["segment_id"]): item for item in transcript}
        projected: list[dict[str, object]] = []
        for dossier in dossiers:
            projected_segments: list[dict[str, object]] = []
            for segment_id in dossier.allowed_segment_ids:
                item = segments.get(segment_id)
                if item is None:
                    raise ValueError("Dossier references unavailable transcript evidence")
                projected_segments.append(
                    {
                        "id": segment_id,
                        "start_ms": item["start_ms"],
                        "end_ms": item["end_ms"],
                        "speaker_id": item["speaker_id"],
                        "text": item["text"],
                    }
                )
            metadata = dossier.model_dump(mode="json")
            metadata["segments"] = projected_segments
            projected.append(metadata)
        return {"dossiers": projected}

    @staticmethod
    def _event_map_without_compatibility(event_map: EventMap) -> dict[str, object]:
        return {
            "user_speaker": event_map.user_speaker.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in event_map.events],
        }

    @staticmethod
    def _untrusted_packet(name: str, payload: object) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encoded = (
            encoded.replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
        return f"<untrusted_{name}>\n{encoded}\n</untrusted_{name}>"
