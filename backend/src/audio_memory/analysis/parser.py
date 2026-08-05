from __future__ import annotations

import json

from pydantic import ValidationError

from audio_memory.prompts.schemas import SceneResult


class SceneOutputError(ValueError):
    pass


def parse_scene_output(raw: str, *, expected_scene: str) -> SceneResult:
    stripped = raw.strip()
    if stripped.startswith("```"):
        raise SceneOutputError("Model output must be raw JSON without Markdown")
    try:
        payload = json.loads(stripped)
        result = SceneResult.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SceneOutputError(str(exc)) from exc
    if result.scene_id != expected_scene:
        raise SceneOutputError(
            f"Expected scene {expected_scene}, received {result.scene_id}"
        )
    if result.should_generate and result.card is None:
        raise SceneOutputError("Generated scene must include a card shell")
    return result

