import pytest
from pydantic import ValidationError

from audio_memory.prompts.beta8_scene_schema import (
    BETA8_SCENE_IDS,
    Beta8SceneResult,
    Beta8UnifiedV1Result,
    normalize_uniquely_misindexed_segment_ids,
    parse_beta8_card_markdown,
    validate_scene_result_ids,
    validate_unified_v1,
    validate_unified_v1_against_index,
)
from audio_memory.prompts.beta8_event_index_schema import Beta8NormalizedEventIndex


def _event_index(
    *, include_second_communication: bool = False,
    include_embedded_event: bool = False,
) -> Beta8NormalizedEventIndex:
    units = [
        {
            "session_id": "comm_1",
            "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "contact_started",
            "start_boundary_evidence_segment_ids": ["seg_0_1"],
            "end_boundary": "contact_ended",
            "end_boundary_evidence_segment_ids": ["seg_0_1"],
            "ranges": [{
                "source_file": "day.md",
                "start_segment_id": "seg_0_1",
                "end_segment_id": "seg_0_1",
            }],
            "subject": "项目周会",
            "description": "讨论交付方案",
        },
        {
            "session_id": "family_1",
            "activity_kind": "conversation",
            "communication_purpose": "non_work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "contact_started",
            "start_boundary_evidence_segment_ids": ["seg_0_2"],
            "end_boundary": "contact_ended",
            "end_boundary_evidence_segment_ids": ["seg_0_2"],
            "ranges": [{
                "source_file": "day.md",
                "start_segment_id": "seg_0_2",
                "end_segment_id": "seg_0_2",
            }],
            "subject": "家庭对话",
            "description": "讨论本周安排",
        },
    ]
    if include_second_communication:
        units.append({
            "session_id": "comm_2",
            "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "contact_started",
            "start_boundary_evidence_segment_ids": ["seg_0_3"],
            "end_boundary": "contact_ended",
            "end_boundary_evidence_segment_ids": ["seg_0_3"],
            "ranges": [{
                "source_file": "day.md",
                "start_segment_id": "seg_0_3",
                "end_segment_id": "seg_0_3",
            }],
            "subject": "客户电话",
            "description": "确认客户需求",
        })
    return Beta8NormalizedEventIndex.model_validate({
        "input_complete": True,
        "input_error": None,
        "primary_sessions": units,
        "embedded_events": ([{
            "event_id": "embedded_demo",
            "parent_session_id": "comm_1",
            "event_kind": "third_party_case",
            "expression_mode": "third_party_case",
            "subject": "演示案例",
            "description": "工作坊中播放的案例",
            "evidence_ranges": [{
                "source_file": "day.md",
                "start_segment_id": "seg_0_1",
                "end_segment_id": "seg_0_1",
            }],
        }] if include_embedded_event else []),
        "coverage_ranges": [{
            "range": unit["ranges"][0],
            "disposition": "session",
            "session_id": unit["session_id"],
            "excluded_reason": None,
            "origin": "model",
        } for unit in units],
        "system_excluded_ranges": [],
    })


def _card(
    *,
    key: str,
    source_unit_ids: list[str],
    basis_type: str = "independent_value",
    communication_kind: str | None = None,
    segment_id: str = "seg_0_2",
) -> dict[str, object]:
    return {
        "draft_card_key": key,
        "card_basis": {
            "type": basis_type,
            "source_unit_ids": source_unit_ids,
            "communication_kind": communication_kind,
        },
        "markdown": f"# {key}\n\n核心信息",
        "source_segment_ids": [segment_id],
        "search_candidates": [],
    }


def _valid_unified_v1_payload() -> dict[str, object]:
    scene_results: list[dict[str, object]] = []
    for scene_id in BETA8_SCENE_IDS:
        cards: list[dict[str, object]] = []
        if scene_id == "work_communication":
            cards = [_card(
                key="work_1",
                source_unit_ids=["comm_1"],
                basis_type="work_communication",
                communication_kind="meeting",
                segment_id="seg_0_1",
            )]
        elif scene_id == "parenting_family":
            cards = [_card(key="family_card", source_unit_ids=["family_1"])]
        scene_results.append({
            "scene_id": scene_id,
            "cards": cards,
            "todo_candidates": [],
            "skip_reason": None if cards else "本场景无独立价值内容",
        })
    return {
        "input_complete": True,
        "input_error": None,
        "scene_results": scene_results,
        "omitted_units": [],
    }


def test_scene_result_keeps_markdown_thin_and_allows_zero_cards() -> None:
    result = Beta8SceneResult(cards=[], todo_candidates=[], skip_reason="没有当前场景内容")
    assert result.cards == []
    assert result.skip_reason == "没有当前场景内容"


def test_scene_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Beta8SceneResult(cards=[], todo_candidates=[], skip_reason="无", hidden="bad")


def test_unified_v1_allows_one_long_work_communication_card() -> None:
    payload = _valid_unified_v1_payload()
    segment_ids = [f"seg_0_{index}" for index in range(2_005)]
    payload["scene_results"][0]["cards"][0]["source_segment_ids"] = segment_ids

    result = Beta8UnifiedV1Result.model_validate(payload)

    assert result.scene_results[0].cards[0].source_segment_ids == segment_ids


def test_scene_result_rejects_unknown_segment_ids_during_runtime_validation() -> None:
    result = Beta8SceneResult.model_validate({
        "cards": [{
            "markdown": "# 标题\n\n核心摘要",
            "source_segment_ids": ["seg-ok", "seg-z", "seg-a"],
            "search_candidates": [],
        }],
        "todo_candidates": [],
        "skip_reason": None,
    })
    with pytest.raises(ValueError, match=r"seg-a, seg-z"):
        validate_scene_result_ids(result, known_segment_ids={"seg-ok"})


def test_uniquely_misindexed_segment_ids_are_corrected_in_all_evidence_fields() -> None:
    result = Beta8SceneResult.model_validate({
        "cards": [{
            "markdown": "# 标题\n\n核心摘要",
            "source_segment_ids": ["seg_3_2686"],
            "search_candidates": [{
                "question": "问题", "purpose": "用途",
                "related_segment_ids": ["seg_3_2686"],
            }],
        }],
        "todo_candidates": [{
            "text": "待办", "owner_type": "user",
            "evidence_segment_ids": ["seg_3_2686"],
        }],
        "skip_reason": None,
    })

    corrected = normalize_uniquely_misindexed_segment_ids(
        result, known_segment_ids={"seg_0_1", "seg_2_2686"}
    )

    assert corrected.cards[0].source_segment_ids == ["seg_2_2686"]
    assert corrected.cards[0].search_candidates[0].related_segment_ids == [
        "seg_2_2686"
    ]
    assert corrected.todo_candidates[0].evidence_segment_ids == ["seg_2_2686"]
    validate_scene_result_ids(
        corrected, known_segment_ids={"seg_0_1", "seg_2_2686"}
    )


def test_ambiguous_segment_index_is_not_silently_corrected() -> None:
    result = Beta8SceneResult.model_validate({
        "cards": [{
            "markdown": "# 标题\n\n核心摘要",
            "source_segment_ids": ["seg_3_9"],
            "search_candidates": [],
        }],
        "todo_candidates": [],
        "skip_reason": None,
    })

    corrected = normalize_uniquely_misindexed_segment_ids(
        result, known_segment_ids={"seg_0_9", "seg_2_9"}
    )

    assert corrected.cards[0].source_segment_ids == ["seg_3_9"]
    with pytest.raises(ValueError, match="seg_3_9"):
        validate_scene_result_ids(
            corrected, known_segment_ids={"seg_0_9", "seg_2_9"}
        )


def test_all_seven_scene_ids_are_exact() -> None:
    assert BETA8_SCENE_IDS == (
        "work_communication",
        "parenting_family",
        "health_state",
        "content_consumption",
        "inspiration_insight",
        "self_growth",
        "life_decisions",
    )


def test_markdown_requires_first_nonempty_h1() -> None:
    with pytest.raises(ValueError, match="first non-empty line"):
        parse_beta8_card_markdown("前言\n\n# 标题\n\n摘要")


def test_markdown_title_and_core_summary_are_extracted_deterministically() -> None:
    parsed = parse_beta8_card_markdown(
        "\n# 方案归属仍需确认\n\n核心摘要\n\n当前**方案**由另一位参与者提出。\n你尚未明确接受。\n\n## 证据\n正文"
    )
    assert parsed.title == "方案归属仍需确认"
    assert parsed.summary == "当前方案由另一位参与者提出。 你尚未明确接受。"


def test_cards_require_null_skip_reason() -> None:
    with pytest.raises(ValidationError, match="skip_reason"):
        Beta8SceneResult.model_validate({
            "cards": [{
                "markdown": "# 标题\n\n摘要",
                "source_segment_ids": [],
                "search_candidates": [],
            }],
            "todo_candidates": [],
            "skip_reason": "错误",
        })


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_order"])
def test_unified_v1_requires_exactly_seven_ordered_scenes(mutation: str) -> None:
    payload = _valid_unified_v1_payload()
    scenes = payload["scene_results"]
    assert isinstance(scenes, list)
    if mutation == "missing":
        scenes.pop()
    elif mutation == "duplicate":
        scenes[-1] = scenes[-2]
    else:
        scenes[1], scenes[2] = scenes[2], scenes[1]

    with pytest.raises(ValueError, match="seven ordered scenes|at least 7"):
        Beta8UnifiedV1Result.model_validate(payload)


def test_unified_v1_json_schema_exposes_hard_bounds_for_routing_ledgers() -> None:
    """The model-visible schema should prevent unbounded non-content JSON."""
    schema = Beta8UnifiedV1Result.model_json_schema()

    assert schema["properties"]["scene_results"]["minItems"] == 7
    assert schema["properties"]["scene_results"]["maxItems"] == 7
    assert schema["properties"]["omitted_units"]["maxItems"] == 384
    assert (
        schema["$defs"]["Beta8CardBasis"]["properties"]["source_unit_ids"][
            "maxItems"
        ]
        == 128
    )
    assert schema["$defs"]["Beta8OmittedUnit"]["properties"]["reason"][
        "maxLength"
    ] == 300


def test_non_work_scene_has_no_card_count_cap() -> None:
    payload = _valid_unified_v1_payload()
    parenting = payload["scene_results"][1]
    parenting["cards"] = [
        _card(key=f"family_{index}", source_unit_ids=["family_1"])
        for index in range(51)
    ]
    parenting["skip_reason"] = None

    result = Beta8UnifiedV1Result.model_validate(payload)

    assert len(result.scene_results[1].cards) == 51
    assert "maxItems" not in str(
        Beta8UnifiedV1Result.model_json_schema()["$defs"]["Beta8UnifiedSceneResult"]
        ["properties"]["cards"]
    )


def test_unified_v1_requires_unique_draft_card_keys() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][1]["cards"][0]["draft_card_key"] = "work_1"

    with pytest.raises(ValueError, match="draft_card_key values must be unique"):
        Beta8UnifiedV1Result.model_validate(payload)


def test_every_index_unit_is_used_or_explicitly_omitted() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][1] = {
        "scene_id": "parenting_family",
        "cards": [],
        "todo_candidates": [],
        "skip_reason": "没有独立价值",
    }
    result = Beta8UnifiedV1Result.model_validate(payload)

    with pytest.raises(ValueError, match="unaccounted index units: family_1"):
        validate_unified_v1_against_index(result, _event_index())

    payload["omitted_units"] = [{"unit_id": "family_1", "reason": "仅有无增量通知"}]
    result = Beta8UnifiedV1Result.model_validate(payload)
    validate_unified_v1_against_index(result, _event_index())


def test_embedded_event_is_routeable_without_creating_a_work_card_obligation() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][0]["cards"][0]["card_basis"]["source_unit_ids"].append(
        "embedded_demo"
    )
    result = Beta8UnifiedV1Result.model_validate(payload)

    validate_unified_v1_against_index(
        result, _event_index(include_embedded_event=True)
    )


def test_every_embedded_fact_must_be_used_or_omitted() -> None:
    result = Beta8UnifiedV1Result.model_validate(_valid_unified_v1_payload())

    with pytest.raises(ValueError, match="unaccounted index units: embedded_demo"):
        validate_unified_v1_against_index(
            result, _event_index(include_embedded_event=True)
        )


def test_index_unit_cannot_be_both_used_and_omitted_or_omitted_twice() -> None:
    payload = _valid_unified_v1_payload()
    payload["omitted_units"] = [{"unit_id": "family_1", "reason": "不成卡"}]
    result = Beta8UnifiedV1Result.model_validate(payload)
    with pytest.raises(ValueError, match="both referenced and omitted"):
        validate_unified_v1_against_index(result, _event_index())

    payload["scene_results"][1] = {
        "scene_id": "parenting_family",
        "cards": [],
        "todo_candidates": [],
        "skip_reason": "无卡",
    }
    payload["omitted_units"] = [
        {"unit_id": "family_1", "reason": "不成卡"},
        {"unit_id": "family_1", "reason": "重复省略"},
    ]
    result = Beta8UnifiedV1Result.model_validate(payload)
    with pytest.raises(ValueError, match="omitted exactly once"):
        validate_unified_v1_against_index(result, _event_index())


def test_work_card_references_exactly_one_work_communication_unit() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][0]["cards"] = [_card(
        key="work_1",
        source_unit_ids=["comm_1", "comm_2"],
        basis_type="work_communication",
        communication_kind="meeting",
        segment_id="seg_0_1",
    )]
    payload["omitted_units"] = [{"unit_id": "family_1", "reason": "不成卡"}]
    result = Beta8UnifiedV1Result.model_validate(payload)

    with pytest.raises(ValueError, match="one work communication"):
        validate_unified_v1_against_index(
            result, _event_index(include_second_communication=True)
        )


def test_same_work_communication_cannot_create_two_cards() -> None:
    payload = _valid_unified_v1_payload()
    work_card = payload["scene_results"][0]["cards"][0]
    payload["scene_results"][0]["cards"].append({**work_card, "draft_card_key": "work_2"})
    result = Beta8UnifiedV1Result.model_validate(payload)

    with pytest.raises(ValueError, match="exactly one card"):
        validate_unified_v1_against_index(result, _event_index())


def test_work_communication_unit_cannot_be_omitted() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][0] = {
        "scene_id": "work_communication",
        "cards": [],
        "todo_candidates": [],
        "skip_reason": "省略工作沟通",
    }
    payload["omitted_units"] = [{"unit_id": "comm_1", "reason": "不成卡"}]
    result = Beta8UnifiedV1Result.model_validate(payload)

    with pytest.raises(ValueError, match="work communication units cannot be omitted"):
        validate_unified_v1_against_index(result, _event_index())


def test_same_project_distinct_work_communications_each_keep_one_card() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][0]["cards"].append(_card(
        key="work_2",
        source_unit_ids=["comm_2"],
        basis_type="work_communication",
        communication_kind="call",
        segment_id="seg_0_3",
    ))
    result = Beta8UnifiedV1Result.model_validate(payload)

    validate_unified_v1_against_index(
        result, _event_index(include_second_communication=True)
    )


def test_work_scene_independent_card_cannot_duplicate_a_work_communication() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][0]["cards"].append(_card(
        key="work_duplicate",
        source_unit_ids=["comm_1"],
        basis_type="independent_value",
        segment_id="seg_0_1",
    ))
    result = Beta8UnifiedV1Result.model_validate(payload)

    with pytest.raises(
        ValueError,
        match="work scene independent value card cannot reference work communication units",
    ):
        validate_unified_v1_against_index(result, _event_index())


def test_other_scene_may_reference_work_communication_for_distinct_value() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][1]["cards"][0]["card_basis"]["source_unit_ids"].append(
        "comm_1"
    )
    result = Beta8UnifiedV1Result.model_validate(payload)

    validate_unified_v1_against_index(result, _event_index())


def test_referenced_work_unit_still_requires_its_one_work_card() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][0] = {
        "scene_id": "work_communication",
        "cards": [],
        "todo_candidates": [],
        "skip_reason": "无工作卡",
    }
    payload["scene_results"][1]["cards"][0]["card_basis"]["source_unit_ids"].append(
        "comm_1"
    )
    result = Beta8UnifiedV1Result.model_validate(payload)

    with pytest.raises(ValueError, match="exactly one work card"):
        validate_unified_v1_against_index(result, _event_index())


def test_unified_v1_rejects_unknown_unit_and_segment_ids() -> None:
    payload = _valid_unified_v1_payload()
    payload["scene_results"][1]["cards"][0]["card_basis"]["source_unit_ids"] = [
        "missing_unit"
    ]
    result = Beta8UnifiedV1Result.model_validate(payload)
    with pytest.raises(ValueError, match="unknown index unit IDs: missing_unit"):
        validate_unified_v1_against_index(result, _event_index())

    payload = _valid_unified_v1_payload()
    payload["scene_results"][1]["cards"][0]["source_segment_ids"] = ["seg_missing"]
    result = Beta8UnifiedV1Result.model_validate(payload)
    with pytest.raises(ValueError, match="Unknown transcript segment IDs: seg_missing"):
        validate_unified_v1(
            result,
            known_segment_ids={"seg_0_1", "seg_0_2"},
            event_index=_event_index(),
        )
