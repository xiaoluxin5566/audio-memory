from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Iterable


RESERVED_MAPPING_TERMS = frozenset({
    "source_unit_ids",
    "work_communication_unit_ids",
    "expected_work_communication_unit_ids",
})


class ReservedMappingPrivacyError(ValueError):
    """A backend-only work-unit mapping escaped into a downstream surface."""


def _privacy_texts(value: object) -> Iterable[str]:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    if isinstance(payload, str):
        yield payload
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        if decoded != payload:
            yield from _privacy_texts(decoded)
        return
    if isinstance(payload, dict):
        for key, item in payload.items():
            yield str(key)
            yield from _privacy_texts(item)
        return
    if isinstance(payload, (list, tuple, set, frozenset)):
        for item in payload:
            yield from _privacy_texts(item)


def project_unified_v1_privacy_surface(value: Any) -> object:
    """Remove only the legitimate V1-to-ledger mapping before privacy scanning."""
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    projected = deepcopy(payload)
    if not isinstance(projected, dict):
        return projected
    projected.pop("omitted_units", None)
    for scene in projected.get("scene_results", []):
        if not isinstance(scene, dict):
            continue
        for card in scene.get("cards", []):
            if not isinstance(card, dict):
                continue
            card.pop("draft_card_key", None)
            basis = card.get("card_basis")
            if isinstance(basis, dict):
                basis.pop("source_unit_ids", None)
    return projected


def sanitized_unified_v1_repair_output(
    raw: str, *, internal_unit_ids: Iterable[str]
) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        close = raw.rfind("}")
        comma = raw.rfind(",", 0, close)
        if close < 0 or comma < 0 or raw[comma + 1:close].strip():
            raise ValueError("unified V1 output cannot be safely projected for repair")
        payload = json.loads(raw[:comma] + raw[comma + 1:])
    projected = project_unified_v1_privacy_surface(payload)
    validate_reserved_mapping_privacy(
        projected,
        internal_unit_ids=internal_unit_ids,
        surface="unified V1 repair projection",
    )
    return json.dumps(projected, ensure_ascii=False, sort_keys=True)


def validate_reserved_mapping_privacy(
    value: object,
    *,
    internal_unit_ids: Iterable[str],
    surface: str,
) -> None:
    forbidden = set(RESERVED_MAPPING_TERMS) | {
        item for item in internal_unit_ids if item
    }
    if any(
        term in text
        for text in _privacy_texts(value)
        for term in forbidden
    ):
        raise ReservedMappingPrivacyError(
            f"reserved work communication mapping leaked into {surface}"
        )


def validate_model_request_privacy(
    request: object,
    *,
    internal_unit_ids: Iterable[str],
    surface: str,
) -> None:
    validate_reserved_mapping_privacy(
        {
            "instructions": getattr(request, "instructions"),
            "user_data": getattr(request, "user_data"),
        },
        internal_unit_ids=internal_unit_ids,
        surface=surface,
    )
