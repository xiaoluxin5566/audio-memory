from __future__ import annotations

import json

from pydantic import TypeAdapter, ValidationError

from audio_memory.prompts.event_schema import EventMapDraft
from audio_memory.prompts.schemas import SceneResultBase, StrictSceneResult


class SceneOutputError(ValueError):
    pass


_SCENE_ADAPTER = TypeAdapter(StrictSceneResult)


def _json_payload(raw: str) -> object:
    stripped = raw.strip()
    if stripped.startswith("```"):
        raise SceneOutputError("Model output must be raw JSON without Markdown")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SceneOutputError(str(exc)) from exc


def parse_event_map_output(raw: str) -> EventMapDraft:
    try:
        return EventMapDraft.model_validate(_json_payload(raw))
    except ValidationError as exc:
        raise SceneOutputError(str(exc)) from exc


def parse_scene_output(raw: str, *, expected_scene: str) -> SceneResultBase:
    try:
        result = _SCENE_ADAPTER.validate_python(_json_payload(raw))
    except ValidationError as exc:
        raise SceneOutputError(str(exc)) from exc
    if result.scene_id != expected_scene:
        raise SceneOutputError(
            f"Expected scene {expected_scene}, received {result.scene_id}"
        )
    return result
