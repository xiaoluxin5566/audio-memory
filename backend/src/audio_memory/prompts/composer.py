from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from hashlib import sha256
from importlib.resources import files

from audio_memory.prompts.event_schema import EventMap
from audio_memory.prompts.store import PROMPT_SCENES, PromptDocument


@dataclass(frozen=True, slots=True)
class ModelRequestPolicy:
    max_tokens: int
    timeout_seconds: float


MODEL_REQUEST_POLICIES = {
    "event-map": ModelRequestPolicy(max_tokens=32_768, timeout_seconds=180),
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
    SCHEMA_VERSION = 2

    @classmethod
    def fixed_rules_hash(cls) -> str:
        fixed = "\n\n".join(
            cls._fixed_prompt(name)
            for name in ("system.md", "event-map.md", "common-scene.md")
        )
        return sha256(fixed.encode("utf-8")).hexdigest()

    def compose_event_map(
        self,
        *,
        transcript: list[dict[str, object]],
        profile: list[dict[str, object]],
        schema: dict[str, object],
    ) -> ModelRequest:
        policy = MODEL_REQUEST_POLICIES["event-map"]
        return ModelRequest(
            scene_id="event-map",
            prompt_version=0,
            schema_version=self.SCHEMA_VERSION,
            system_rules=self._fixed_prompt("system.md"),
            common_rules=self._fixed_prompt("event-map.md"),
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

    def compose_scene(
        self,
        scene_id: str,
        *,
        transcript: list[dict[str, object]],
        event_map: EventMap,
        profile: list[dict[str, object]],
        prompt: PromptDocument,
        schema: dict[str, object],
    ) -> ModelRequest:
        if scene_id not in PROMPT_SCENES or prompt.scene_id != scene_id:
            raise ValueError("Prompt scene does not match request scene")
        policy = MODEL_REQUEST_POLICIES["scene"]
        return ModelRequest(
            scene_id=scene_id,
            prompt_version=prompt.version,
            schema_version=self.SCHEMA_VERSION,
            system_rules=self._fixed_prompt("system.md"),
            common_rules=self._fixed_prompt("common-scene.md"),
            scene_prompt=prompt.content,
            user_data="\n".join(
                [
                    self._untrusted_packet("transcript_data", transcript),
                    self._untrusted_packet(
                        "event_map", event_map.model_dump(mode="json")
                    ),
                    self._untrusted_packet("profile_data", profile),
                ]
            ),
            schema_json=self._schema_json(schema),
            max_tokens=policy.max_tokens,
            timeout_seconds=policy.timeout_seconds,
            segment_count=len(transcript),
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
    def _untrusted_packet(name: str, payload: object) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encoded = (
            encoded.replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
        return f"<untrusted_{name}>\n{encoded}\n</untrusted_{name}>"
