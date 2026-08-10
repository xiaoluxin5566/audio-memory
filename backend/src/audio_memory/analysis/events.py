from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.analysis.parser import SceneOutputError


T = TypeVar("T")


async def request_with_one_repair(
    *,
    client,
    request,
    provider_snapshot: dict[str, object],
    parse: Callable[[str], T],
    invalid_code: str = "model_response_invalid",
) -> T:
    """Send a strict request and permit exactly one schema/JSON repair."""

    raw = await client.generate(
        str(provider_snapshot["provider_id"]),
        system=request.rendered_instructions,
        user=request.user_data,
        model_id=str(provider_snapshot["model_id"]),
        scene_id=request.scene_id,
        max_tokens=request.max_tokens,
        timeout_seconds=request.timeout_seconds,
        segment_count=request.segment_count,
        repair_attempted=False,
    )
    try:
        return parse(raw)
    except (SceneOutputError, ValueError) as first_error:
        repair = await client.generate(
            str(provider_snapshot["provider_id"]),
            system=(
                "修复不符合 JSON Schema 的模型输出。只返回修复后的原始 JSON，"
                "不要 Markdown，不要解释。\n"
                f"JSON Schema：\n{request.schema_json}"
            ),
            user=json.dumps(
                {
                    "validation_error": str(first_error),
                    "invalid_model_output": raw,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            model_id=str(provider_snapshot["model_id"]),
            scene_id=request.scene_id,
            max_tokens=request.max_tokens,
            timeout_seconds=request.timeout_seconds,
            segment_count=request.segment_count,
            repair_attempted=True,
        )
        try:
            return parse(repair)
        except (SceneOutputError, ValueError) as second_error:
            raise ProviderAnalysisError(
                "Provider returned output that violates the required schema",
                code=invalid_code,
            ) from second_error
