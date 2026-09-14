from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from audio_memory.experiments.beta8_v1_ab import (
    BETA8_SCENE_IDS,
    Beta8UnifiedV1Result,
    build_blind_map,
    review_variant,
    validate_unified_v1,
)


def _card(*, title: str = "产品周会", unit_key: str = "communication-1") -> dict:
    return {
        "card_basis": {
            "type": "work_communication",
            "unit_key": unit_key,
            "communication_kind": "meeting",
        },
        "markdown": (
            f"# {title}\n\n"
            "模型切换方向已经确定，评测资源仍需落实。\n\n"
            "## 关键结论\n\n"
            "| 议题 | 结论 |\n|---|---|\n| 模型 | 分阶段切换 |\n\n"
            "## 下一步\n\n- 确认评测负责人。"
        ),
        "source_segment_ids": ["seg_0_1"],
        "search_candidates": [],
    }


def valid_payload() -> dict:
    results = []
    for scene_id in BETA8_SCENE_IDS:
        if scene_id == "work_communication":
            results.append(
                {
                    "scene_id": scene_id,
                    "cards": [_card()],
                    "todo_candidates": [],
                    "skip_reason": None,
                }
            )
        else:
            results.append(
                {
                    "scene_id": scene_id,
                    "cards": [],
                    "todo_candidates": [],
                    "skip_reason": "没有足够独立价值",
                }
            )
    return {
        "input_complete": True,
        "input_error": None,
        "scene_results": results,
        "plan_corrections": [],
    }


def test_unified_result_requires_exactly_seven_ordered_scenes() -> None:
    payload = valid_payload()
    payload["scene_results"].pop()
    with pytest.raises(ValueError, match="exactly seven"):
        Beta8UnifiedV1Result.model_validate(payload)


def test_unified_result_rejects_duplicate_scene() -> None:
    payload = valid_payload()
    payload["scene_results"][1]["scene_id"] = "work_communication"
    with pytest.raises(ValueError, match="ordered scene IDs"):
        Beta8UnifiedV1Result.model_validate(payload)


def test_validation_rejects_unknown_segment_ids() -> None:
    result = Beta8UnifiedV1Result.model_validate(valid_payload())
    with pytest.raises(ValueError, match="Unknown transcript segment IDs"):
        validate_unified_v1(result, known_segment_ids={"seg_0_2"})


def test_validation_rejects_duplicate_work_communication_keys() -> None:
    payload = valid_payload()
    payload["scene_results"][0]["cards"].append(_card(title="另一张卡"))
    result = Beta8UnifiedV1Result.model_validate(payload)
    with pytest.raises(ValueError, match="Duplicate work communication unit_key"):
        validate_unified_v1(result, known_segment_ids={"seg_0_1"})


def test_validation_rejects_manually_numbered_section_heading() -> None:
    payload = valid_payload()
    payload["scene_results"][0]["cards"][0]["markdown"] = (
        "# 产品周会\n\n已确定切换方向。\n\n## 一、关键结论\n\n内容。"
    )
    result = Beta8UnifiedV1Result.model_validate(payload)
    with pytest.raises(ValueError, match="manual ordinal"):
        validate_unified_v1(result, known_segment_ids={"seg_0_1"})


def test_review_reports_structure_without_claiming_semantic_correctness() -> None:
    result = Beta8UnifiedV1Result.model_validate(valid_payload())
    review = review_variant(result)
    assert review.card_count == 1
    assert review.heading_count == 2
    assert review.table_count == 1
    assert review.semantic_correctness is None


def test_blind_map_is_reproducible_and_balanced() -> None:
    assert build_blind_map("stable-seed") == build_blind_map("stable-seed")
    assert set(build_blind_map("stable-seed").values()) == {"variant-a", "variant-b"}


def test_result_round_trips_as_json() -> None:
    result = Beta8UnifiedV1Result.model_validate(valid_payload())
    restored = Beta8UnifiedV1Result.model_validate_json(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    )
    assert restored == result


def test_parser_normalizes_card_scoped_todos_without_a_repair_call() -> None:
    from audio_memory.experiments.beta8_v1_ab import parse_unified_response

    payload = valid_payload()
    payload["scene_results"][0]["cards"][0]["todo_candidates"] = [
        {
            "text": "确认评测负责人",
            "owner_type": "user",
            "assignee_text": None,
            "due_at": None,
            "due_text": None,
            "evidence_segment_ids": ["seg_0_1"],
        }
    ]
    result = parse_unified_response(json.dumps(payload, ensure_ascii=False))

    assert result.scene_results[0].todo_candidates[0].text == "确认评测负责人"


def test_parser_clears_meaningless_identity_fields_from_non_work_cards() -> None:
    from audio_memory.experiments.beta8_v1_ab import parse_unified_response

    payload = valid_payload()
    parenting = payload["scene_results"][1]
    parenting["skip_reason"] = None
    parenting["cards"] = [
        {
            "card_basis": {
                "type": "independent_value",
                "unit_key": "parenting_1",
                "communication_kind": None,
            },
            "markdown": "# 亲子沟通\n\n先确认孩子是不会，还是已经过载。",
            "source_segment_ids": ["seg_0_1"],
            "search_candidates": [],
        }
    ]
    result = parse_unified_response(json.dumps(payload, ensure_ascii=False))

    basis = result.scene_results[1].cards[0].card_basis
    assert basis.type == "independent_value"
    assert basis.unit_key is None
    assert basis.communication_kind is None


def test_prompt_manifest_includes_shared_scene_boundaries() -> None:
    from audio_memory.experiments.beta8_v1_ab import ExperimentPromptSet

    manifest = ExperimentPromptSet().manifest()
    assert "scene-boundaries.md" in manifest


def test_variant_requests_share_transcript_and_final_contract() -> None:
    from audio_memory.experiments.beta8_v1_ab import ExperimentPromptSet

    prompts = ExperimentPromptSet()
    transcript = '<segment id="seg_0_1">hello</segment>'
    request_a = prompts.compose_variant_a(transcript)
    plan_request = prompts.compose_variant_b_plan(transcript)
    request_b = prompts.compose_variant_b_write(
        transcript,
        {
            "input_complete": True,
            "input_error": None,
            "card_briefs": [],
            "scene_skip_reasons": {
                scene_id: "none" for scene_id in BETA8_SCENE_IDS
            },
        },
    )

    assert transcript in request_a.user_data
    assert transcript in plan_request.user_data
    assert transcript in request_b.user_data
    assert request_a.schema_json == request_b.schema_json
    assert request_a.instructions != request_b.instructions
    assert "scene-boundaries.md" not in request_a.instructions
    assert request_a.thinking_enabled is True
    assert plan_request.thinking_enabled is False
    assert request_b.thinking_enabled is True


def test_planning_brief_rejects_an_unbounded_evidence_dump() -> None:
    from audio_memory.experiments.beta8_v1_ab import Beta8PlanCardBrief

    with pytest.raises(ValueError, match="too_long"):
        Beta8PlanCardBrief.model_validate(
            {
                "brief_key": "brief-1",
                "scene_id": "work_communication",
                "card_basis": {
                    "type": "work_communication",
                    "unit_key": "meeting-1",
                    "communication_kind": "meeting",
                },
                "core_value": "保存会议决策",
                "required_topics": [],
                "attribution_notes": [],
                "source_segment_ids": [f"seg_0_{index}" for index in range(81)],
            }
        )


def test_lightweight_event_map_has_only_routing_and_bounded_anchors() -> None:
    from audio_memory.experiments.beta8_v1_ab import Beta8LightweightEventMap

    payload = {
        "input_complete": True,
        "input_error": None,
        "work_communications": [
            {
                "unit_key": "meeting-1",
                "communication_kind": "meeting",
                "purpose": "同步模型切换方案",
                "topics": ["切换顺序", "评测资源"],
                "start_segment_id": "seg_0_1",
                "end_segment_id": "seg_0_9",
                "anchor_segment_ids": ["seg_0_1", "seg_0_9"],
                "attribution_note": None,
            }
        ],
        "other_candidates": [],
        "scene_skip_reasons": {
            scene_id: (None if scene_id == "work_communication" else "无独立价值")
            for scene_id in BETA8_SCENE_IDS
        },
    }
    event_map = Beta8LightweightEventMap.model_validate(payload)

    serialized = event_map.model_dump(mode="json")
    text = json.dumps(serialized, ensure_ascii=False)
    assert "markdown" not in text
    assert "todo_candidates" not in text
    assert "search_candidates" not in text

    payload["work_communications"][0]["anchor_segment_ids"] = [
        f"seg_0_{index}" for index in range(13)
    ]
    with pytest.raises(ValueError, match="too_long"):
        Beta8LightweightEventMap.model_validate(payload)


def test_planning_transcript_keeps_ids_with_less_than_one_third_wrapper_size() -> None:
    from audio_memory.experiments.beta8_v1_ab import (
        planning_transcript_markdown,
        transcript_markdown,
    )

    rows = [
        {
            "segment_id": f"seg_0_{index}",
            "file_name": "sample.mp3",
            "file_position": 0,
            "segment_index": index,
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
            "text": "一句简短的转写",
        }
        for index in range(100)
    ]
    verbose = transcript_markdown(rows)
    compact = planning_transcript_markdown(rows)

    assert "seg_0_0" in compact and "seg_0_99" in compact
    assert "sample.mp3" in compact
    assert len(compact) < len(verbose) / 3


@dataclass
class _Diagnostic:
    provider_id: str
    model_id: str
    scene_id: str
    input_tokens: int = 100
    output_tokens: int = 50
    elapsed_seconds: float = 1.25


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.request_diagnostics: list[_Diagnostic] = []

    async def generate(self, provider_id: str, **kwargs) -> str:
        self.calls.append({"provider_id": provider_id, **kwargs})
        stage = kwargs["scene_id"]
        self.request_diagnostics.append(
            _Diagnostic(provider_id, kwargs["model_id"], stage)
        )
        if stage == "beta8-v1-ab-variant-b-plan":
            payload = {
                "input_complete": True,
                "input_error": None,
                "work_communications": [],
                "other_candidates": [],
                "scene_skip_reasons": {
                    scene_id: "none" for scene_id in BETA8_SCENE_IDS
                },
            }
        else:
            payload = valid_payload()
        return json.dumps(payload, ensure_ascii=False)


class _FailingPlanProvider(_FakeProvider):
    async def generate(self, provider_id: str, **kwargs) -> str:
        if kwargs["scene_id"] == "beta8-v1-ab-variant-b-plan":
            self.request_diagnostics.append(
                _Diagnostic(provider_id, kwargs["model_id"], kwargs["scene_id"])
            )
            raise RuntimeError("truncated")
        return await super().generate(provider_id, **kwargs)


@pytest.mark.asyncio
async def test_runner_executes_one_call_then_two_calls_and_writes_artifacts(
    tmp_path,
) -> None:
    from audio_memory.experiments.beta8_v1_ab import Beta8V1ABRunner

    provider = _FakeProvider()
    rows = [
        {
            "segment_id": "seg_0_1",
            "file_name": "sample.mp3",
            "file_position": 0,
            "segment_index": 1,
            "start_ms": 0,
            "end_ms": 1000,
            "text": "hello",
        }
    ]
    summary = await Beta8V1ABRunner(provider).run(rows=rows, output=tmp_path)

    assert [call["scene_id"] for call in provider.calls] == [
        "beta8-v1-ab-variant-a-write",
        "beta8-v1-ab-variant-b-plan",
        "beta8-v1-ab-variant-b-write",
    ]
    assert summary["variant-a"]["call_count"] == 1
    assert summary["variant-b"]["call_count"] == 2
    assert summary["variant-a"]["input_tokens"] == 100
    assert summary["variant-b"]["input_tokens"] == 200
    assert (tmp_path / "variant-a" / "result.json").exists()
    assert (tmp_path / "variant-b" / "plan.json").exists()
    assert (tmp_path / "comparison.html").exists()
    html = (tmp_path / "comparison.html").read_text(encoding="utf-8")
    assert "方案 X" in html and "方案 Y" in html
    assert "variant-a" not in html and "variant-b" not in html


@pytest.mark.asyncio
async def test_runner_resumes_from_saved_variant_a_without_repeating_paid_call(
    tmp_path,
) -> None:
    from audio_memory.experiments.beta8_v1_ab import Beta8V1ABRunner

    (tmp_path / "variant-a").mkdir(parents=True)
    (tmp_path / "variant-a" / "raw.txt").write_text(
        json.dumps(valid_payload(), ensure_ascii=False), encoding="utf-8"
    )
    provider = _FakeProvider()
    rows = [
        {
            "segment_id": "seg_0_1",
            "file_name": "sample.mp3",
            "file_position": 0,
            "segment_index": 1,
            "start_ms": 0,
            "end_ms": 1000,
            "text": "hello",
        }
    ]
    summary = await Beta8V1ABRunner(provider).run(
        rows=rows,
        output=tmp_path,
        resume_variant_a=True,
        resumed_variant_a_elapsed_seconds=361.003,
    )

    assert [call["scene_id"] for call in provider.calls] == [
        "beta8-v1-ab-variant-b-plan",
        "beta8-v1-ab-variant-b-write",
    ]
    assert summary["variant-a"]["call_count"] == 1
    assert summary["variant-a"]["elapsed_seconds"] == 361.003
    assert summary["variant-a"]["input_tokens"] is None


@pytest.mark.asyncio
async def test_runner_persists_failed_plan_diagnostics_before_raising(tmp_path) -> None:
    from audio_memory.experiments.beta8_v1_ab import Beta8V1ABRunner

    rows = [
        {
            "segment_id": "seg_0_1",
            "file_name": "sample.mp3",
            "file_position": 0,
            "segment_index": 1,
            "start_ms": 0,
            "end_ms": 1000,
            "text": "hello",
        }
    ]
    with pytest.raises(RuntimeError, match="truncated"):
        await Beta8V1ABRunner(_FailingPlanProvider()).run(
            rows=rows, output=tmp_path
        )

    payload = json.loads(
        (tmp_path / "variant-b" / "plan-stage-diagnostics.json").read_text()
    )
    assert payload[0]["scene_id"] == "beta8-v1-ab-variant-b-plan"


@pytest.mark.asyncio
async def test_runner_materializes_both_saved_variants_without_model_calls(
    tmp_path,
) -> None:
    from audio_memory.experiments.beta8_v1_ab import Beta8V1ABRunner

    for variant in ("variant-a", "variant-b"):
        (tmp_path / variant).mkdir(parents=True)
        (tmp_path / variant / "raw.txt").write_text(
            json.dumps(valid_payload(), ensure_ascii=False), encoding="utf-8"
        )
    (tmp_path / "variant-b" / "plan.json").write_text(
        json.dumps(
            {
                "input_complete": True,
                "input_error": None,
                "work_communications": [],
                "other_candidates": [],
                "scene_skip_reasons": {
                    scene_id: "none" for scene_id in BETA8_SCENE_IDS
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    provider = _FakeProvider()
    rows = [
        {
            "segment_id": "seg_0_1",
            "file_name": "sample.mp3",
            "file_position": 0,
            "segment_index": 1,
            "start_ms": 0,
            "end_ms": 1000,
            "text": "hello",
        }
    ]
    await Beta8V1ABRunner(provider).run(
        rows=rows,
        output=tmp_path,
        resume_variant_a=True,
        resume_variant_b=True,
    )

    assert provider.calls == []
    assert (tmp_path / "comparison.html").exists()
