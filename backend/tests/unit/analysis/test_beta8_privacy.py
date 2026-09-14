import pytest

from audio_memory.analysis.beta8_privacy import (
    project_unified_v1_privacy_surface,
    validate_reserved_mapping_privacy,
)


def test_json_unicode_escape_cannot_hide_reserved_mapping_value() -> None:
    raw = r'{"bad":"\u0063ommunication-1"}'

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        validate_reserved_mapping_privacy(
            raw,
            internal_unit_ids={"communication-1"},
            surface="model output",
        )


def test_json_unicode_escape_cannot_hide_reserved_mapping_field_name() -> None:
    raw = r'{"\u0073ource_unit_ids":[]}'

    with pytest.raises(ValueError, match="reserved work communication mapping"):
        validate_reserved_mapping_privacy(
            raw,
            internal_unit_ids=set(),
            surface="model output",
        )


def test_v1_privacy_projection_removes_legitimate_omitted_unit_ledger() -> None:
    projected = project_unified_v1_privacy_surface({
        "input_complete": True,
        "scene_results": [],
        "omitted_units": [
            {"unit_id": "u31", "reason": "无独立帮助价值"},
        ],
    })

    assert "omitted_units" not in projected
    validate_reserved_mapping_privacy(
        projected,
        internal_unit_ids={"u31"},
        surface="unified V1 downstream-visible output",
    )
