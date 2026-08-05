from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from importlib.resources import files

from audio_memory.prompts.event_schema import EventMap
from audio_memory.prompts.schemas import SceneResult
from audio_memory.prompts.store import PROMPT_SCENES, PromptDocument


@dataclass(frozen=True, slots=True)
class ModelRequest:
    scene_id: str
    prompt_version: int
    schema_version: int
    system_rules: str
    scene_prompt: str
    user_data: str
    schema_json: str
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

    def compose_event_map(
        self,
        *,
        transcript: list[dict[str, object]],
        profile: list[dict[str, object]],
        schema: dict[str, object],
    ) -> ModelRequest:
        return ModelRequest(
            scene_id="event-map",
            prompt_version=0,
            schema_version=self.SCHEMA_VERSION,
            system_rules=self._fixed_prompt("system.md"),
            common_rules=self._fixed_prompt("event-map.md"),
            scene_prompt="",
            user_data="\n".join(
                [
                    self._untrusted_packet("transcript_data", transcript),
                    self._untrusted_packet("profile_data", profile),
                ]
            ),
            schema_json=self._schema_json(schema),
        )

    def compose(
        self,
        scene_id: str,
        *,
        transcript: str,
        profile: list[dict[str, object]],
        prompt: PromptDocument,
    ) -> ModelRequest:
        """Phase-zero runner adapter; Task 5 switches to compose_scene."""
        if scene_id not in PROMPT_SCENES or prompt.scene_id != scene_id:
            raise ValueError("Prompt scene does not match request scene")
        fixed_rules = "\n\n".join(
            [self._fixed_prompt("system.md"), self._fixed_prompt("common-scene.md")]
        )
        return ModelRequest(
            scene_id=scene_id,
            prompt_version=prompt.version,
            schema_version=1,
            system_rules=fixed_rules,
            scene_prompt=prompt.content,
            user_data="\n".join(
                [
                    self._untrusted_packet("transcript_data", transcript),
                    self._untrusted_packet("profile_data", profile),
                ]
            ),
            schema_json=self._schema_json(SceneResult.model_json_schema()),
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
        )

    @staticmethod
    def _fixed_prompt(name: str) -> str:
        return files("audio_memory.prompts").joinpath(name).read_text().strip()

    @staticmethod
    def _schema_json(schema: dict[str, object]) -> str:
        return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _untrusted_packet(name: str, payload: object) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encoded = (
            encoded.replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
        return f"<untrusted_{name}>\n{encoded}\n</untrusted_{name}>"
