from __future__ import annotations

import asyncio
import builtins
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

import audio_memory.analysis.beta8_evaluation as beta8_evaluation
from audio_memory.analysis.beta8_evaluation import (
    EXPECTED_DOUBAO_MANIFEST,
    EvaluationPaths,
    mark_paid_execution,
    prepare_dry_run,
    final_audit_score,
    load_json_object,
    parse_merged_transcript,
    require_paid_confirmation,
    validate_doubao_manifest,
    validate_ground_truth,
)
from audio_memory.analysis.pipeline_state import ModelCallMetric
from audio_memory.prompts.beta8_pipeline_schema import AggregatedAudit
from audio_memory.prompts.beta8_event_index_schema import Beta8NormalizedEventIndex


def test_real_evaluation_refuses_to_start_without_paid_confirmation() -> None:
    with pytest.raises(PermissionError, match="paid model calls"):
        require_paid_confirmation(False)


def test_real_evaluation_accepts_explicit_paid_confirmation() -> None:
    require_paid_confirmation(True)


def _boundary_index(
    ranges_by_unit: list[list[tuple[str, str, str]]],
    *, include_embedded_work_media: bool = False,
) -> Beta8NormalizedEventIndex:
    primary_sessions = [
        {
            "session_id": f"work-{index}",
            "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "contact_started",
            "start_boundary_evidence_segment_ids": [ranges[0][1]],
            "end_boundary": "contact_ended",
            "end_boundary_evidence_segment_ids": [ranges[-1][2]],
            "ranges": [
                {
                    "source_file": source_file,
                    "start_segment_id": start_id,
                    "end_segment_id": end_id,
                }
                for source_file, start_id, end_id in ranges
            ],
            "subject": f"沟通 {index}",
            "description": "真实工作沟通。",
        }
        for index, ranges in enumerate(ranges_by_unit, start=1)
    ]
    return Beta8NormalizedEventIndex.model_validate({
        "input_complete": True,
        "input_error": None,
        "primary_sessions": primary_sessions,
        "embedded_events": ([{
            "event_id": "embedded-work-media",
            "parent_session_id": "work-1",
            "event_kind": "content_playback",
            "expression_mode": "media_playback",
            "subject": "媒体中的工作问答",
            "description": "主会话内播放的工作相关内容。",
            "evidence_ranges": [primary_sessions[0]["ranges"][0]],
        }] if include_embedded_work_media else []),
        "coverage_ranges": [
            {
                "range": source_range,
                "disposition": "session",
                "session_id": session["session_id"],
                "excluded_reason": None,
                "origin": "model",
            }
            for session in primary_sessions
            for source_range in session["ranges"]
        ],
        "system_excluded_ranges": [],
    })


def _boundary_ground_truth() -> dict[str, object]:
    return {
        "work_communications": [
            {
                "event_id": "call-1",
                "ranges": [{
                    "file_name": "a.mp3",
                    "start_segment_id": "seg_0_10",
                    "end_segment_id": "seg_0_20",
                }],
            },
            {
                "event_id": "call-2",
                "ranges": [{
                    "file_name": "a.mp3",
                    "start_segment_id": "seg_0_30",
                    "end_segment_id": "seg_0_40",
                }],
            },
            {
                "event_id": "cross-file-workshop",
                "ranges": [
                    {
                        "file_name": "a.mp3",
                        "start_segment_id": "seg_0_50",
                        "end_segment_id": "seg_0_60",
                    },
                    {
                        "file_name": "b.mp3",
                        "start_segment_id": "seg_1_0",
                        "end_segment_id": "seg_1_20",
                    },
                ],
            },
        ]
    }


def test_event_index_boundary_gate_accepts_one_unit_per_real_communication() -> None:
    index = _boundary_index([
        [("a.mp3", "seg_0_8", "seg_0_22")],
        [("a.mp3", "seg_0_30", "seg_0_40")],
        [
            ("a.mp3", "seg_0_50", "seg_0_60"),
            ("b.mp3", "seg_1_0", "seg_1_20"),
        ],
    ])

    result = beta8_evaluation.validate_event_index_communication_boundaries(
        index, _boundary_ground_truth()
    )

    assert result == {
        "expected_work_communication_count": 3,
        "actual_work_communication_count": 3,
        "matched_event_ids": ["call-1", "call-2", "cross-file-workshop"],
    }


def test_boundary_gate_matches_canonical_file_ids_to_truth_file_names_by_segment_id() -> None:
    index = _boundary_index([
        [("internal-file-a", "seg_0_8", "seg_0_22")],
        [("internal-file-a", "seg_0_30", "seg_0_40")],
        [
            ("internal-file-a", "seg_0_50", "seg_0_60"),
            ("internal-file-b", "seg_1_0", "seg_1_20"),
        ],
    ])

    result = beta8_evaluation.validate_event_index_communication_boundaries(
        index, _boundary_ground_truth()
    )

    assert result["matched_event_ids"] == [
        "call-1", "call-2", "cross-file-workshop"
    ]


def test_ground_truth_boundary_gate_ignores_embedded_work_like_media() -> None:
    index = _boundary_index([
        [("a.mp3", "seg_0_8", "seg_0_22")],
        [("a.mp3", "seg_0_30", "seg_0_40")],
        [
            ("a.mp3", "seg_0_50", "seg_0_60"),
            ("b.mp3", "seg_1_0", "seg_1_20"),
        ],
    ], include_embedded_work_media=True)

    result = beta8_evaluation.validate_event_index_communication_boundaries(
        index, _boundary_ground_truth()
    )

    assert result["actual_work_communication_count"] == 3


@pytest.mark.parametrize("count", [0, 1, 3])
def test_ground_truth_boundary_gate_supports_arbitrary_sample_counts(
    count: int,
) -> None:
    ranges = [
        [("a.mp3", f"seg_0_{index * 10}", f"seg_0_{index * 10 + 5}")]
        for index in range(1, count + 1)
    ]
    truth = {
        "work_communications": [
            {
                "event_id": f"event-{index}",
                "ranges": [{
                    "file_name": source_file,
                    "start_segment_id": start_id,
                    "end_segment_id": end_id,
                } for source_file, start_id, end_id in unit_ranges],
            }
            for index, unit_ranges in enumerate(ranges, start=1)
        ]
    }

    result = beta8_evaluation.validate_event_index_communication_boundaries(
        _boundary_index(ranges), truth
    )

    assert result["expected_work_communication_count"] == count
    assert result["actual_work_communication_count"] == count


def test_event_index_boundary_gate_rejects_extra_work_topic_as_communication() -> None:
    index = _boundary_index([
        [("a.mp3", "seg_0_8", "seg_0_22")],
        [("a.mp3", "seg_0_30", "seg_0_40")],
        [
            ("a.mp3", "seg_0_50", "seg_0_60"),
            ("b.mp3", "seg_1_0", "seg_1_20"),
        ],
        [("a.mp3", "seg_0_70", "seg_0_80")],
    ])

    with pytest.raises(ValueError, match="expected 3 work communications, got 4"):
        beta8_evaluation.validate_event_index_communication_boundaries(
            index, _boundary_ground_truth()
        )


def test_event_index_boundary_gate_rejects_balanced_merge_and_split_errors() -> None:
    index = _boundary_index([
        [("a.mp3", "seg_0_8", "seg_0_42")],
        [("a.mp3", "seg_0_50", "seg_0_60")],
        [("b.mp3", "seg_1_0", "seg_1_20")],
    ])

    with pytest.raises(ValueError, match="communication boundary mismatch"):
        beta8_evaluation.validate_event_index_communication_boundaries(
            index, _boundary_ground_truth()
        )


@pytest.mark.parametrize("first_ranges", [
    [("a.mp3", "seg_0_15", "seg_0_15")],
    [("a.mp3", "seg_0_11", "seg_0_20")],
    [("a.mp3", "seg_0_10", "seg_0_19")],
    [("a.mp3", "seg_0_10", "seg_0_14"),
     ("a.mp3", "seg_0_16", "seg_0_20")],
])
def test_boundary_gate_rejects_partial_coverage_despite_correct_count(first_ranges) -> None:
    index = _boundary_index([
        first_ranges,
        [("a.mp3", "seg_0_30", "seg_0_40")],
        [("a.mp3", "seg_0_50", "seg_0_60"),
         ("b.mp3", "seg_1_0", "seg_1_20")],
    ])
    with pytest.raises(ValueError, match="communication boundary mismatch"):
        beta8_evaluation.validate_event_index_communication_boundaries(
            index, _boundary_ground_truth()
        )


def test_boundary_gate_accepts_adjacent_ranges_covering_whole_communication() -> None:
    index = _boundary_index([
        [("a.mp3", "seg_0_10", "seg_0_14"),
         ("a.mp3", "seg_0_15", "seg_0_20")],
        [("a.mp3", "seg_0_30", "seg_0_40")],
        [("a.mp3", "seg_0_50", "seg_0_60"),
         ("b.mp3", "seg_1_0", "seg_1_20")],
    ])
    result = beta8_evaluation.validate_event_index_communication_boundaries(
        index, _boundary_ground_truth()
    )
    assert result["matched_event_ids"] == ["call-1", "call-2", "cross-file-workshop"]


def _content_acceptance_fixture():
    payload = _boundary_index([
        [("a.mp3", "seg_0_0", "seg_0_19")],
        [("a.mp3", "seg_0_20", "seg_0_39")],
        [("a.mp3", "seg_0_40", "seg_0_59")],
    ]).model_dump(mode="json")
    payload["primary_sessions"][1]["communication_purpose"] = "non_work"
    payload["primary_sessions"][2].update(
        activity_kind="content_playback", communication_purpose="not_applicable",
        participation_mode="recorded_or_broadcast_content",
    )
    def span(start, end):
        return {"file_name": "a.mp3", "start_segment_id": f"seg_0_{start}",
                "end_segment_id": f"seg_0_{end}"}
    payload["embedded_events"] = [{
        "event_id": "case-embedded", "parent_session_id": "work-1",
        "event_kind": "third_party_case", "expression_mode": "third_party_case",
        "subject": "同事甲", "description": "会议中举出的同事案例。",
        "evidence_ranges": [{"source_file": "a.mp3", "start_segment_id": "seg_0_5",
                             "end_segment_id": "seg_0_7"}],
    }]
    truth = {
        "work_communications": [{"event_id": "work", "ranges": [span(0, 19)]}],
        "must_cover_media_units": [{"unit_id": "media", "ranges": [span(40, 59)],
                                     "must_preserve": ["媒体不是用户经历"]}],
        "third_party_cases": [{"case_id": "case", "ranges": [span(5, 7)],
                               "must_preserve": ["同事甲不是用户"]}],
        "low_value_exclusions": [{"exclusion_id": "family", "ranges": [span(20, 25)],
            "expected_handling": "index_as_substantive_family_unit_but_do_not_create_standalone_card"}],
    }
    return payload, truth


@pytest.mark.parametrize("mode,accepted", [("user_experience", True), ("media_playback", False), ("third_party_case", False)])
def test_family_fact_can_be_embedded_but_not_broadcast_or_third_party(mode, accepted):
    payload, truth = _content_acceptance_fixture()
    payload["primary_sessions"][1].update(
        activity_kind="content_playback", communication_purpose="not_applicable",
        participation_mode="recorded_or_broadcast_content",
    )
    payload["embedded_events"].append({
        "event_id": "family-interaction", "parent_session_id": "work-2",
        "event_kind": "discussion_topic", "expression_mode": mode,
        "subject": "家庭交流", "description": "播放过程中讨论孩子的作业。",
        "evidence_ranges": [{"source_file": "a.mp3", "start_segment_id": "seg_0_20",
                             "end_segment_id": "seg_0_25"}],
    })
    report = beta8_evaluation.evaluate_event_index_acceptance(
        Beta8NormalizedEventIndex.model_validate(payload), truth)
    family = next(item for item in report["checks"] if item["fact_id"] == "family")
    assert family["range_and_type_passed"] is accepted
    assert report["semantic_review_status"] == "pending"


def test_content_acceptance_keeps_semantic_review_pending_after_structural_pass() -> None:
    payload, truth = _content_acceptance_fixture()
    report = beta8_evaluation.evaluate_event_index_acceptance(
        Beta8NormalizedEventIndex.model_validate(payload), truth
    )
    assert report["blockers"] == []
    assert report["semantic_review_status"] == "pending"
    assert {item["fact_id"] for item in report["checks"]} == {"media", "case", "family"}


@pytest.mark.parametrize("problem", ["missing_case", "wrong_case_owner", "partial_case", "wrong_media", "missing_family", "family_as_media"])
def test_content_acceptance_rejects_known_fact_loss_or_wrong_type(problem) -> None:
    payload, truth = _content_acceptance_fixture()
    if problem == "missing_case":
        payload["embedded_events"] = []
    elif problem == "wrong_case_owner":
        payload["embedded_events"][0]["expression_mode"] = "user_experience"
    elif problem == "partial_case":
        payload["embedded_events"][0]["evidence_ranges"][0]["end_segment_id"] = "seg_0_5"
    elif problem == "wrong_media":
        payload["primary_sessions"][2].update(
            activity_kind="solo_speech_or_activity", participation_mode="solo_user_activity"
        )
    elif problem == "family_as_media":
        payload["primary_sessions"][1].update(
            activity_kind="content_playback", participation_mode="recorded_or_broadcast_content",
            communication_purpose="not_applicable",
        )
    else:
        payload["primary_sessions"].pop(1)
    report = beta8_evaluation.evaluate_event_index_acceptance(
        Beta8NormalizedEventIndex.model_validate(payload), truth
    )
    assert report["blockers"]


def test_content_acceptance_does_not_require_embedded_event_quota() -> None:
    payload, truth = _content_acceptance_fixture()
    payload["embedded_events"] = []
    truth["third_party_cases"] = []
    report = beta8_evaluation.evaluate_event_index_acceptance(
        Beta8NormalizedEventIndex.model_validate(payload), truth
    )
    assert report["blockers"] == []


def test_index_gate_saves_private_failure_evidence_before_raising(tmp_path) -> None:
    script = load_evaluation_script()
    payload, truth = _content_acceptance_fixture()
    payload["embedded_events"] = []
    with pytest.raises(ValueError, match="case"):
        script.save_index_acceptance(Beta8NormalizedEventIndex.model_validate(payload), truth, tmp_path)
    outputs = list(tmp_path.glob("index-acceptance-*.json"))
    assert len(outputs) == 1
    assert outputs[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(outputs[0].read_text())["automatic_status"] == "failed"


def test_v1_fact_review_distinguishes_parent_reference_from_fact_evidence() -> None:
    payload, truth = _content_acceptance_fixture()
    v1 = {"scene_results": [{"scene_id": "work_communication", "cards": [{
        "draft_card_key": "work-card", "markdown": "工作卡正文",
        "card_basis": {"source_unit_ids": ["work-1"]},
        "source_segment_ids": ["seg_0_0"],
    }]}], "omitted_units": [{"unit_id": "case-embedded", "reason": "案例用于理解会议，不单独成卡"}]}
    report = beta8_evaluation.build_v1_fact_review(
        v1, Beta8NormalizedEventIndex.model_validate(payload), truth
    )
    case = next(item for item in report["facts"] if item["fact_id"] == "case")
    assert case["related_card_keys"] == ["work-card"]
    assert case["direct_evidence_card_keys"] == []
    assert case["omissions"] == v1["omitted_units"]
    assert report["semantic_review_status"] == "pending"
    v1["scene_results"][0]["cards"][0]["source_segment_ids"].append("seg_0_5")
    updated = beta8_evaluation.build_v1_fact_review(
        v1, Beta8NormalizedEventIndex.model_validate(payload), truth
    )
    case = next(item for item in updated["facts"] if item["fact_id"] == "case")
    assert case["direct_evidence_card_keys"] == ["work-card"]
    assert updated["semantic_review_status"] == "pending"


def test_missing_provider_cost_is_null_not_zero() -> None:
    metric = ModelCallMetric(
        run_id="run-1",
        stage="event_index",
        attempt_kind="normal",
        provider="deepseek",
        model="deepseek-v4-pro",
        input_tokens=100,
        output_tokens=20,
        duration_ms=1_000,
        cost=None,
        cost_unavailable_reason="provider did not report cost",
        checkpoint_reused=False,
    )

    assert metric.cost is None
    assert metric.cost_unavailable_reason == "provider did not report cost"


def test_final_audit_score_is_a_strict_publish_gate_score() -> None:
    passed = AggregatedAudit.model_validate({
        "audit_phase": "final",
        "expected_audit_unit_ids": ["final-1"],
        "completed_audit_unit_ids": ["final-1"],
        "incomplete_audit_unit_ids": [],
        "issues": [],
    })
    incomplete = passed.model_copy(update={
        "completed_audit_unit_ids": [],
        "incomplete_audit_unit_ids": ["final-1"],
    })

    assert final_audit_score(passed) == 100
    assert final_audit_score(incomplete) is None


def test_merged_transcript_parser_preserves_file_order_and_timestamps(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.md"
    source.write_text(
        "# 全天逐字稿\n\n"
        "## 上午.mp3 逐字稿\n\n"
        "[00:00:01 - 00:00:03] 第一段\n\n"
        "[00:01:02.500 - 00:01:04] 第二段\n\n"
        "## 下午.aac 逐字稿\n\n"
        "[01:00:00 - 01:00:02] 第三段\n",
        encoding="utf-8",
    )

    rows = parse_merged_transcript(source)

    assert rows == [
        {
            "segment_id": "seg_0_0",
            "file_name": "上午.mp3",
            "file_position": 0,
            "segment_index": 0,
            "start_ms": 1_000,
            "end_ms": 3_000,
            "text": "第一段",
        },
        {
            "segment_id": "seg_0_1",
            "file_name": "上午.mp3",
            "file_position": 0,
            "segment_index": 1,
            "start_ms": 62_500,
            "end_ms": 64_000,
            "text": "第二段",
        },
        {
            "segment_id": "seg_1_0",
            "file_name": "下午.aac",
            "file_position": 1,
            "segment_index": 0,
            "start_ms": 3_600_000,
            "end_ms": 3_602_000,
            "text": "第三段",
        },
    ]


def test_merged_transcript_parser_rejects_text_before_a_file_heading(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid.md"
    source.write_text("[00:00:01 - 00:00:02] 无归属内容\n", encoding="utf-8")

    with pytest.raises(ValueError, match="file heading"):
        parse_merged_transcript(source)


def test_merged_transcript_parser_accepts_volcano_headings_without_suffix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "volcano.md"
    source.write_text(
        "# volcano 云端转写\n\n"
        "## 07月31日 11-11 Pokee SE-audio.mp3\n\n"
        "[00:00:06 - 00:00:08] 第一段\n",
        encoding="utf-8",
    )

    rows = parse_merged_transcript(source)

    assert rows[0]["file_name"] == "07月31日 11-11 Pokee SE-audio.mp3"
    assert rows[0]["segment_id"] == "seg_0_0"


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "beta8"
MANIFEST_PATH = FIXTURE_ROOT / "doubao-long-audio-2-manifest.json"
GROUND_TRUTH_PATH = FIXTURE_ROOT / "doubao-long-audio-2-ground-truth.json"
SCRIPT_PATH = Path(__file__).parents[4] / "tests" / "evaluate-beta8-indexed-v1.py"
MAIN_PATH = Path(__file__).parents[3] / "src/audio_memory/main.py"


def valid_doubao_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider": "豆包",
        "model": "录音文件识别 2.0",
        "resource_id": "volc.seedasr.auc",
        "file_count": 4,
        "segment_count": 6373,
        "sha256": "6cb6881073d769f91639eb478d5c8dadf96be0431016885ddddd7e8f16cb20b8",
        "ground_truth_sha256": (
            "c232c736e42c0676d8c77d3250030ba025542ec73f79d78e0d9b75210fba8cb0"
        ),
        "files": [
            {"file_name": "a.mp3", "segment_count": 624},
            {"file_name": "b.mp3", "segment_count": 1627},
            {"file_name": "c.mp3", "segment_count": 3246},
            {"file_name": "d.mp3", "segment_count": 876},
        ],
    }


def test_ground_truth_gate_is_injected_only_by_the_acceptance_script() -> None:
    main_source = MAIN_PATH.read_text(encoding="utf-8")
    script_source = SCRIPT_PATH.read_text(encoding="utf-8")
    production_constructor = main_source[main_source.index("Beta8ReportRunner(") :]
    production_constructor = production_constructor[:production_constructor.index(")")]

    assert "event_index_gate" not in production_constructor
    assert "event_index_gate=lambda index" in script_source


def real_ground_truth_inputs() -> tuple[
    dict[str, object], dict[str, object], list[dict[str, object]]
]:
    manifest = load_json_object(MANIFEST_PATH)
    ground_truth = load_json_object(GROUND_TRUTH_PATH)
    transcript = Path(str(manifest["transcript_path"]))
    return manifest, ground_truth, parse_merged_transcript(transcript)


def load_evaluation_script():
    spec = importlib.util.spec_from_file_location(
        "beta8_evaluation_cli_direct_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_ground_truth(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_evaluation_refuses_whisper_manifest_for_doubao_acceptance() -> None:
    manifest = valid_doubao_manifest() | {"provider": "local_whisper"}

    with pytest.raises(ValueError, match="Doubao"):
        validate_doubao_manifest(manifest)


def test_manifest_requires_fixed_asr_identity_counts_and_sha256() -> None:
    manifest = load_json_object(MANIFEST_PATH)

    validated = validate_doubao_manifest(manifest)

    assert validated["provider"] == "豆包"
    assert validated["model"] == "录音文件识别 2.0"
    assert validated["resource_id"] == "volc.seedasr.auc"
    assert validated["file_count"] == 4
    assert validated["segment_count"] == 6373
    assert validated["sha256"] == EXPECTED_DOUBAO_MANIFEST["sha256"]
    assert validated["ground_truth_sha256"] == (
        "c232c736e42c0676d8c77d3250030ba025542ec73f79d78e0d9b75210fba8cb0"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", str(validated["sha256"]))
    assert re.fullmatch(
        r"[0-9a-f]{64}", str(validated["ground_truth_sha256"])
    )
    assert sha256(GROUND_TRUTH_PATH.read_bytes()).hexdigest() == validated[
        "ground_truth_sha256"
    ]
    assert sum(int(item["segment_count"]) for item in validated["files"]) == 6373


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("model", "whisper-large-v3-turbo"),
        ("schema_version", 2),
        ("resource_id", "whisper"),
        ("file_count", 6),
        ("segment_count", 5199),
        ("sha256", "0" * 64),
        ("ground_truth_sha256", "0" * 64),
    ],
)
def test_manifest_rejects_non_doubao_acceptance_identity(
    field: str, invalid: object
) -> None:
    manifest = valid_doubao_manifest() | {field: invalid}

    with pytest.raises(ValueError, match=field):
        validate_doubao_manifest(manifest)


def test_ground_truth_is_grounded_and_contains_no_precomputed_report() -> None:
    manifest, ground_truth, segments = real_ground_truth_inputs()

    validated = validate_ground_truth(ground_truth, manifest, segments)

    assert validated["source_sha256"] == manifest["sha256"]
    assert validated["work_communications"]
    assert any(item["cross_file"] for item in validated["work_communications"])
    assert validated["must_cover_media_units"]
    assert validated["third_party_cases"]
    assert validated["low_value_exclusions"]
    assert "expected_report_markdown" not in json.dumps(
        validated, ensure_ascii=False
    )


def test_ground_truth_locks_adjudicated_ids_and_boundary_snippets() -> None:
    manifest, ground_truth, segments = real_ground_truth_inputs()

    validated = validate_ground_truth(ground_truth, manifest, segments)
    segment_by_id = {str(item["segment_id"]): item for item in segments}
    work = {item["event_id"]: item for item in validated["work_communications"]}
    media = {item["unit_id"]: item for item in validated["must_cover_media_units"]}

    assert set(work) == {
        "work-highlight-window-and-xiaohongshu-sdk-call",
        "work-mobile-reporting-switch-coordination",
        "work-vehicle-program-schedule-call",
        "work-vehicle-test-slot-followup-call",
        "work-glasses-okr-and-always-on-product-workshop",
    }
    assert set(media) == {
        "media-august-horoscope-playback",
        "podcast-smart-swimming-goggles-founder-interview",
        "podcast-new-labs-and-ai-for-ai",
        "media-infinite-fiction-author-interview",
        "media-human-versus-go-ai-commentary",
    }
    assert {item["case_id"] for item in validated["third_party_cases"]} == {
        "third-party-chen-zhen-departure-and-work-state"
    }
    assert {
        item["exclusion_id"] for item in validated["low_value_exclusions"]
    } == {
        "simple-weekday-and-birthday-calculation",
        "unreflected-game-system-announcements",
    }

    workshop_ranges = work[
        "work-glasses-okr-and-always-on-product-workshop"
    ]["ranges"]
    assert workshop_ranges[0]["start_segment_id"] == "seg_1_1338"
    assert workshop_ranges[-1]["end_segment_id"] == "seg_2_2258"
    assert segment_by_id["seg_1_1338"]["text"] == (
        "哦来了，那你没啥投投你 okr 那个呢？"
    )
    assert segment_by_id["seg_2_2258"]["text"] == "可以可以，走了，拜拜。"

    fiction_range = media["media-infinite-fiction-author-interview"]["ranges"][0]
    assert fiction_range["start_segment_id"] == "seg_2_2285"
    assert fiction_range["end_segment_id"] == "seg_2_2296"
    assert "无限恐怖" in str(segment_by_id["seg_2_2285"]["text"])
    assert segment_by_id["seg_2_2296"]["text"] == (
        "大家也可以在评论区聊聊你最早是从哪来。"
    )

    go_range = media["media-human-versus-go-ai-commentary"]["ranges"][0]
    assert go_range["start_segment_id"] == "seg_3_385"
    assert go_range["end_segment_id"] == "seg_3_523"
    assert segment_by_id["seg_3_385"]["text"] == (
        "想不到已经2026年，人类征服 AI 的欲望丝毫没有减弱。"
    )
    assert segment_by_id["seg_3_523"]["text"] == (
        "最后一关用的是 AI 并没有膨胀。"
    )


@pytest.mark.parametrize(
    ("collection", "field"),
    [
        ("must_cover_media_units", "speaker_owner"),
        ("must_cover_media_units", "must_preserve"),
        ("third_party_cases", "subject"),
        ("third_party_cases", "expected_attribution"),
        ("third_party_cases", "forbidden_attribution"),
        ("low_value_exclusions", "reason"),
        ("low_value_exclusions", "expected_handling"),
    ],
)
def test_ground_truth_rejects_deleted_category_semantics(
    collection: str, field: str
) -> None:
    manifest, ground_truth, segments = real_ground_truth_inputs()
    mutated = deepcopy(ground_truth)
    del mutated[collection][0][field]

    with pytest.raises(ValueError, match=field):
        validate_ground_truth(mutated, manifest, segments)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("labels", []),
        ("dimensions", []),
        ("hard_blockers", ["主体错误"]),
        ("review_fields", ["reviewer"]),
    ],
)
def test_ground_truth_rejects_incomplete_blind_review_contract(
    field: str, invalid: object
) -> None:
    manifest, ground_truth, segments = real_ground_truth_inputs()
    mutated = deepcopy(ground_truth)
    mutated["human_blind_review"][field] = invalid

    with pytest.raises(ValueError, match=field):
        validate_ground_truth(mutated, manifest, segments)


@pytest.mark.parametrize(
    "collection",
    [
        "work_communications",
        "must_cover_media_units",
        "third_party_cases",
        "low_value_exclusions",
    ],
)
def test_ground_truth_rejects_empty_required_acceptance_category(
    collection: str,
) -> None:
    manifest, ground_truth, segments = real_ground_truth_inputs()
    mutated = deepcopy(ground_truth)
    mutated[collection] = []

    with pytest.raises(ValueError, match=collection):
        validate_ground_truth(mutated, manifest, segments)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("labels", ["left", "right"]),
        ("scale", {"minimum": 1, "maximum": 4}),
        ("dimensions", ["完整覆盖与防漏"]),
        (
            "hard_blockers",
            [
                "主体错误",
                "工作沟通拆分或误合并",
                "未解决事实错误",
                "搜索来源错配",
            ],
        ),
        (
            "review_fields",
            [
                "reviewer",
                "label",
                "dimension",
                "score",
                "evidence",
                "notes",
                "extra",
            ],
        ),
    ],
)
def test_ground_truth_rejects_changed_reviewed_blind_review_contract(
    field: str, invalid: object
) -> None:
    manifest, ground_truth, segments = real_ground_truth_inputs()
    mutated = deepcopy(ground_truth)
    mutated["human_blind_review"][field] = invalid

    with pytest.raises(ValueError, match=field):
        validate_ground_truth(mutated, manifest, segments)


def test_ground_truth_rejects_unknown_segment_and_prewritten_report() -> None:
    manifest = valid_doubao_manifest()
    segments = [
        {
            "segment_id": "seg_0_0",
            "file_name": "a.mp3",
            "file_position": 0,
            "segment_index": 0,
            "start_ms": 1,
            "end_ms": 2,
            "text": "可核对内容",
        }
    ]
    ground_truth = {
        "schema_version": 1,
        "source_sha256": manifest["sha256"],
        "work_communications": [
            {
                "event_id": "work-1",
                "communication_kind": "phone_call",
                "cross_file": False,
                "ranges": [
                    {
                        "file_name": "a.mp3",
                        "start_segment_id": "seg_missing",
                        "end_segment_id": "seg_0_0",
                    }
                ],
                "must_include_topics": ["真实主题"],
            }
        ],
        "must_cover_media_units": [],
        "third_party_cases": [],
        "low_value_exclusions": [],
        "human_blind_review": {"expected_report_markdown": "# 预写答案"},
    }

    with pytest.raises(ValueError, match="prewritten report|unknown segment"):
        validate_ground_truth(ground_truth, manifest, segments)


def test_prepare_dry_run_is_offline_and_does_not_create_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry-run attempted network access")

    monkeypatch.setattr(socket, "create_connection", refuse_network)
    output_root = tmp_path / "acceptance-output"
    paths = EvaluationPaths(
        transcript=Path(
            "/Users/liujinxin/Documents/音频Always on Demo/outputs/"
            "cloud-asr-july31/volcano/正式报告链路输入逐字稿.md"
        ),
        manifest=MANIFEST_PATH,
        ground_truth=GROUND_TRUTH_PATH,
        output_root=output_root,
    )

    first = prepare_dry_run(paths)
    second = prepare_dry_run(paths)

    assert first["mode"] == "dry-run"
    assert first["network_accessed"] is False
    assert first["writes_performed"] is False
    assert first["input"]["sha256"] == EXPECTED_DOUBAO_MANIFEST["sha256"]
    assert first["input"]["ground_truth_sha256"] == (
        EXPECTED_DOUBAO_MANIFEST["ground_truth_sha256"]
    )
    assert first["input"]["file_count"] == 4
    assert first["input"]["segment_count"] == 6373
    assert first["prompt_binding"]["fixed_rules_hash"]
    assert len(first["prompt_binding"]["prompt_hashes"]) >= 2
    assert first["expected_stages"]["normal_v1_model_calls"] == [
        "event_index",
        "all_scenes_v1",
    ]
    assert first["expected_stages"]["required_model_stages"] == [
        "event_index",
        "all_scenes_v1",
        "initial_audit",
        "global_editorial_review",
        "final_audit",
    ]
    assert first["expected_stages"]["conditional_model_stages"] == [
        "targeted_revision"
    ]
    assert first["expected_stages"]["conditional_non_model_stages"] == [
        "native_search"
    ]
    assert first["expected_stages"]["repair_attempt_kinds"] == [
        "schema_repair",
        "retry",
        "resume",
    ]
    assert first["run_id"] != second["run_id"]
    assert Path(first["planned_output_directory"]).parent == output_root
    assert not output_root.exists()


def test_prepare_dry_run_stops_before_planning_on_hash_mismatch(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "transcript.md"
    transcript.write_text(
        "# volcano 云端转写\n\n## a.mp3\n\n"
        "[00:00:00 - 00:00:01] 被篡改的输入\n",
        encoding="utf-8",
    )
    paths = EvaluationPaths(
        transcript=transcript,
        manifest=MANIFEST_PATH,
        ground_truth=GROUND_TRUTH_PATH,
        output_root=tmp_path / "out",
    )

    with pytest.raises(ValueError, match="sha256"):
        prepare_dry_run(paths)


@pytest.mark.parametrize(
    "collection",
    [
        "work_communications",
        "must_cover_media_units",
        "third_party_cases",
        "low_value_exclusions",
    ],
)
def test_prepare_dry_run_rejects_reduced_ground_truth_before_planning(
    tmp_path: Path, collection: str
) -> None:
    manifest, ground_truth, _segments = real_ground_truth_inputs()
    mutated = deepcopy(ground_truth)
    mutated[collection].pop()
    ground_truth_path = tmp_path / f"reduced-{collection}.json"
    write_ground_truth(ground_truth_path, mutated)
    output_root = tmp_path / "out"
    paths = EvaluationPaths(
        transcript=Path(str(manifest["transcript_path"])),
        manifest=MANIFEST_PATH,
        ground_truth=ground_truth_path,
        output_root=output_root,
    )

    with pytest.raises(ValueError, match="Ground Truth sha256"):
        prepare_dry_run(paths)

    assert not output_root.exists()


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("scale", {"minimum": 0, "maximum": 10}),
        ("dimensions", ["完整覆盖与防漏"]),
    ],
)
def test_prepare_dry_run_rejects_changed_blind_review_before_planning(
    tmp_path: Path, field: str, invalid: object
) -> None:
    manifest, ground_truth, _segments = real_ground_truth_inputs()
    mutated = deepcopy(ground_truth)
    mutated["human_blind_review"][field] = invalid
    ground_truth_path = tmp_path / f"changed-{field}.json"
    write_ground_truth(ground_truth_path, mutated)
    output_root = tmp_path / "out"
    paths = EvaluationPaths(
        transcript=Path(str(manifest["transcript_path"])),
        manifest=MANIFEST_PATH,
        ground_truth=ground_truth_path,
        output_root=output_root,
    )

    with pytest.raises(ValueError, match="Ground Truth sha256"):
        prepare_dry_run(paths)

    assert not output_root.exists()


def test_prepare_dry_run_rejects_non_doubao_provenance(
    tmp_path: Path,
) -> None:
    manifest = load_json_object(MANIFEST_PATH)
    provenance = tmp_path / "README.txt"
    provenance.write_text(
        "模型：mlx-community/whisper-large-v3-turbo\n"
        "资源 ID：local.whisper\n",
        encoding="utf-8",
    )
    manifest["provenance_path"] = str(provenance)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    paths = EvaluationPaths(
        transcript=Path(str(manifest["transcript_path"])),
        manifest=manifest_path,
        ground_truth=GROUND_TRUTH_PATH,
        output_root=tmp_path / "out",
    )

    with pytest.raises(ValueError, match="provenance.*Doubao"):
        prepare_dry_run(paths)


def test_paid_result_metadata_records_network_and_writes() -> None:
    dry_run = {
        "mode": "dry-run",
        "network_accessed": False,
        "writes_performed": False,
        "run_id": "run-1",
    }

    paid = mark_paid_execution(dry_run)

    assert paid["mode"] == "paid-run"
    assert paid["network_accessed"] is True
    assert paid["writes_performed"] is True
    assert dry_run["mode"] == "dry-run"


def test_real_evaluation_requires_explicit_confirmation_wording() -> None:
    with pytest.raises(PermissionError, match="explicit confirmation"):
        require_paid_confirmation(False)


def test_dry_run_cli_prints_plan_without_creating_output(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "cli-output"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--dry-run",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["writes_performed"] is False
    assert plan["network_accessed"] is False
    assert not output_root.exists()


def test_flash_smoke_dry_run_records_nonfinal_execution_binding(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "flash-smoke-output"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--dry-run",
            "--validation-tier",
            "mechanical-smoke",
            "--report-model",
            "deepseek-v4-flash",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["execution_binding"] == {
        "validation_tier": "mechanical-smoke",
        "qualifies_as_final_acceptance": False,
        "report_provider": "deepseek",
        "report_model": "deepseek-v4-flash",
        "search_provider": "kimi",
        "search_model": "kimi-k2.6",
    }
    assert not output_root.exists()


def test_stop_after_v1_dry_run_records_the_paid_execution_control(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "v1-only-output"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--dry-run",
            "--stop-after-v1",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["execution_control"] == {
        "stop_after_event_index_checkpoint": False,
        "stop_after_v1_checkpoint": True,
        "provider_transient_total_attempts": 1,
    }
    assert not output_root.exists()


def test_stop_after_index_dry_run_records_one_call_execution_control(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "index-only-output"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--dry-run",
            "--stop-after-index",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["execution_control"] == {
        "stop_after_event_index_checkpoint": True,
        "stop_after_v1_checkpoint": False,
        "provider_transient_total_attempts": 1,
    }
    assert not output_root.exists()


@pytest.mark.parametrize("replacement_kind", ["reduced", "replaced"])
def test_dry_run_cli_rejects_noncanonical_ground_truth(
    tmp_path: Path, replacement_kind: str
) -> None:
    _manifest, ground_truth, _segments = real_ground_truth_inputs()
    if replacement_kind == "reduced":
        ground_truth["work_communications"].pop()
        replacement = ground_truth
    else:
        replacement = {"schema_version": 1}
    ground_truth_path = tmp_path / f"{replacement_kind}.json"
    write_ground_truth(ground_truth_path, replacement)
    output_root = tmp_path / "cli-output"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--dry-run",
            "--ground-truth",
            str(ground_truth_path),
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "Ground Truth sha256" in completed.stderr
    assert not output_root.exists()


def test_paid_cli_refuses_run_without_confirmed_before_creating_output(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paid-output"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--run-paid",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "explicit confirmation" in completed.stderr
    assert not output_root.exists()


def test_validation_tier_binds_flash_smoke_without_qualifying_as_final() -> None:
    module = load_evaluation_script()
    args = SimpleNamespace(
        validation_tier="mechanical-smoke",
        report_provider="deepseek",
        report_model="deepseek-v4-flash",
        search_provider="kimi",
        search_model="kimi-k2.6",
    )

    binding = module.validate_execution_binding(args)

    assert binding == {
        "validation_tier": "mechanical-smoke",
        "qualifies_as_final_acceptance": False,
        "report_provider": "deepseek",
        "report_model": "deepseek-v4-flash",
        "search_provider": "kimi",
        "search_model": "kimi-k2.6",
    }


def test_validation_tier_rejects_flash_as_final_acceptance() -> None:
    module = load_evaluation_script()
    args = SimpleNamespace(
        validation_tier="final",
        report_provider="deepseek",
        report_model="deepseek-v4-flash",
        search_provider="kimi",
        search_model="kimi-k2.6",
    )

    with pytest.raises(ValueError, match="Final acceptance.*deepseek-v4-pro"):
        module.validate_execution_binding(args)


def test_response_quarantine_is_private_and_metadata_contains_no_model_text(
    tmp_path: Path,
) -> None:
    import stat

    module = load_evaluation_script()
    store = module.ResponseQuarantine(tmp_path)
    raw = '{"private":"不得出现在元数据"}'

    record = store.capture("all_scenes_v1", raw)

    response_path = Path(record["response_path"])
    metadata_path = Path(record["metadata_path"])
    assert response_path.read_text(encoding="utf-8") == raw
    assert stat.S_IMODE(response_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(response_path.parent.stat().st_mode) == 0o700
    metadata_text = metadata_path.read_text(encoding="utf-8")
    assert "不得出现在元数据" not in metadata_text
    metadata = json.loads(metadata_text)
    assert metadata["stage"] == "all_scenes_v1"
    assert metadata["validation_status"] == "unvalidated"
    assert metadata["response_sha256"] == sha256(raw.encode()).hexdigest()
    assert metadata["response_bytes"] == len(raw.encode())


@pytest.mark.asyncio
async def test_seed_paid_run_persists_flash_smoke_model_identity(tmp_path: Path) -> None:
    from audio_memory.analysis.pipeline_identity import validate_version_pipeline_identity
    from audio_memory.db import Database
    from audio_memory.models import AnalysisVersion

    module = load_evaluation_script()
    database = Database(tmp_path / "flash-smoke.sqlite3")
    await database.create_schema()

    version_id = await module.seed_paid_run(
        database,
        [],
        report_provider="deepseek",
        report_model="deepseek-v4-flash",
        search_provider="kimi",
        search_model="kimi-k2.6",
    )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
    identity = validate_version_pipeline_identity(version)
    assert (version.provider_id, version.model_id) == (
        "deepseek",
        "deepseek-v4-flash",
    )
    assert (identity.search_provider_id, identity.search_model_id) == (
        "kimi",
        "kimi-k2.6",
    )
    await database.dispose()


@pytest.mark.parametrize(
    ("run_paid", "confirmed"),
    [(False, True), (True, False)],
)
def test_run_paid_direct_call_requires_both_gates_before_setup_or_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_paid: bool,
    confirmed: bool,
) -> None:
    module = load_evaluation_script()
    output_root = tmp_path / "direct-paid-output"
    paths = EvaluationPaths(
        transcript=Path("/must-not-be-read"),
        manifest=Path("/must-not-be-read"),
        ground_truth=Path("/must-not-be-read"),
        output_root=output_root,
    )
    setup_calls: list[str] = []

    def refuse_setup(_paths: EvaluationPaths) -> None:
        setup_calls.append("prepare_dry_run")
        raise AssertionError("paid setup reached before gate")

    original_import = builtins.__import__

    def refuse_paid_import(
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: object = (),
        level: int = 0,
    ):
        if name == "httpx" or name.startswith("audio_memory.providers.keychain"):
            raise AssertionError(f"paid import reached before gate: {name}")
        return original_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(module, "prepare_dry_run", refuse_setup)
    monkeypatch.setattr(builtins, "__import__", refuse_paid_import)
    args = SimpleNamespace(run_paid=run_paid, confirmed=confirmed)

    with pytest.raises(PermissionError, match="--run-paid.*explicit confirmation"):
        asyncio.run(module.run_paid(args, paths))

    assert setup_calls == []
    assert not output_root.exists()


@pytest.mark.asyncio
async def test_failed_paid_run_is_persisted_and_clears_running_state(
    tmp_path: Path,
) -> None:
    from audio_memory.analysis.errors import ProviderAnalysisError
    from audio_memory.db import Database
    from audio_memory.models import AnalysisJob, AnalysisVersion

    module = load_evaluation_script()
    output = tmp_path / "paid-failure"
    output.mkdir()
    database = Database(output / "evaluation.sqlite3")
    await database.create_schema()
    version_id = await module.seed_paid_run(database, [])
    plan = {
        "mode": "dry-run",
        "network_accessed": False,
        "writes_performed": False,
        "run_id": "run-failed",
    }
    error = ProviderAnalysisError(
        "Provider output was truncated", code="model_output_truncated"
    )

    result = await module.record_failed_paid_run(
        database,
        output=output,
        plan=plan,
        version_id=version_id,
        error=error,
    )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
        job = await session.get(AnalysisJob, version.source_job_id)
    saved = json.loads((output / "evaluation-result.json").read_text())
    assert version.status == "failed"
    assert version.error_code == "model_output_truncated"
    assert job.stage == "failed"
    assert result == saved
    assert saved["mode"] == "paid-run"
    assert saved["status"] == "failed"
    assert saved["error"] == {
        "type": "ProviderAnalysisError",
        "code": "model_output_truncated",
        "message": "Provider output was truncated",
    }
    await database.dispose()


@pytest.mark.asyncio
async def test_intentional_v1_pause_is_persisted_as_resumable_not_failed_result(
    tmp_path: Path,
) -> None:
    from audio_memory.analysis.beta8_runner import Beta8CheckpointPause
    from audio_memory.db import Database
    from audio_memory.models import AnalysisVersion

    module = load_evaluation_script()
    output = tmp_path / "paid-v1-pause"
    output.mkdir()
    database = Database(output / "evaluation.sqlite3")
    await database.create_schema()
    version_id = await module.seed_paid_run(database, [])
    plan = {
        "mode": "dry-run",
        "network_accessed": False,
        "writes_performed": False,
        "run_id": "run-v1-pause",
    }

    result = await module.record_checkpoint_pause(
        database,
        output=output,
        plan=plan,
        version_id=version_id,
        pause=Beta8CheckpointPause(
            "Paused after saving the all_scenes_v1 checkpoint"
        ),
    )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
    saved = json.loads((output / "evaluation-result.json").read_text())
    assert version.status == "failed"
    assert version.error_code == "intentional_v1_checkpoint_pause"
    assert version.worker_owner_id is None
    assert result == saved
    assert saved["status"] == "paused"
    assert saved["checkpoint_stage"] == "all_scenes_v1"
    assert saved["resumable"] is True
    await database.dispose()


@pytest.mark.asyncio
async def test_intentional_index_pause_records_event_index_checkpoint(
    tmp_path: Path,
) -> None:
    from audio_memory.analysis.beta8_runner import Beta8CheckpointPause
    from audio_memory.db import Database

    module = load_evaluation_script()
    output = tmp_path / "paid-index-pause"
    output.mkdir()
    database = Database(output / "evaluation.sqlite3")
    await database.create_schema()
    version_id = await module.seed_paid_run(database, [])

    result = await module.record_checkpoint_pause(
        database,
        output=output,
        plan={"run_id": "run-index-pause"},
        version_id=version_id,
        pause=Beta8CheckpointPause(
            "Paused after saving the event_index checkpoint",
            checkpoint_stage="event_index",
        ),
    )

    assert result["status"] == "paused"
    assert result["checkpoint_stage"] == "event_index"
    assert result["resumable"] is True
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_paid_run_requires_same_bound_inputs_and_saved_index(
    tmp_path: Path,
) -> None:
    from audio_memory.db import Database
    from audio_memory.models import AnalysisVersion

    module = load_evaluation_script()
    output = tmp_path / "run-existing"
    output.mkdir()
    database = Database(output / "evaluation.sqlite3")
    await database.create_schema()
    version_id = await module.seed_paid_run(database, [])
    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
        rules_hash = version.fixed_rules_hash
        version.staged_results_json = json.dumps({"beta8_event_index": {"saved": True}})
        await session.commit()
    saved_plan = {
        "mode": "dry-run",
        "run_id": "run-existing",
        "planned_output_directory": str(output.resolve()),
        "input": {"sha256": "transcript-hash", "segment_count": 6373},
        "ground_truth": {"work_communication_count": 5},
        "prompt_binding": {"fixed_rules_hash": rules_hash},
        "expected_stages": {"normal_v1_model_calls": ["event_index", "all_scenes_v1"]},
    }
    (output / "acceptance-plan.json").write_text(json.dumps(saved_plan))

    plan, resumed_version_id = await module.prepare_resume_paid_run(
        database,
        output=output,
        current_plan=deepcopy(saved_plan),
        report_provider="deepseek",
        report_model="deepseek-v4-pro",
        search_provider="kimi",
        search_model="kimi-k2.6",
    )

    assert plan == saved_plan
    assert resumed_version_id == version_id

    drifted_plan = deepcopy(saved_plan)
    drifted_plan["input"]["sha256"] = "changed-transcript"
    with pytest.raises(ValueError, match="input binding changed"):
        await module.prepare_resume_paid_run(
            database,
            output=output,
            current_plan=drifted_plan,
            report_provider="deepseek",
            report_model="deepseek-v4-pro",
            search_provider="kimi",
            search_model="kimi-k2.6",
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_resume_preflight_reclaims_a_failed_checkpointed_version(
    tmp_path: Path,
) -> None:
    from audio_memory.db import Database
    from audio_memory.models import AnalysisJob, AnalysisVersion

    module = load_evaluation_script()
    output = tmp_path / "run-failed"
    output.mkdir()
    database = Database(output / "evaluation.sqlite3")
    await database.create_schema()
    version_id = await module.seed_paid_run(database, [])
    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
        job = await session.get(AnalysisJob, version.source_job_id)
        rules_hash = version.fixed_rules_hash
        version.status = "failed"
        version.error_code = "model_response_invalid"
        version.worker_owner_id = None
        version.completed_at = "2026-09-07T00:00:00+00:00"
        version.staged_results_json = json.dumps({"beta8_event_index": {"saved": True}})
        job.stage = "failed"
        job.error_code = "model_response_invalid"
        await session.commit()
    plan = {
        "mode": "dry-run",
        "run_id": "run-failed",
        "planned_output_directory": str(output.resolve()),
        "input": {"sha256": "transcript-hash"},
        "ground_truth": {},
        "prompt_binding": {"fixed_rules_hash": rules_hash},
        "expected_stages": {},
    }
    (output / "acceptance-plan.json").write_text(json.dumps(plan))

    await module.prepare_resume_paid_run(
        database,
        output=output,
        current_plan=deepcopy(plan),
        report_provider="deepseek",
        report_model="deepseek-v4-pro",
        search_provider="kimi",
        search_model="kimi-k2.6",
    )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
        job = await session.get(AnalysisJob, version.source_job_id)
    assert version.status == "running"
    assert version.error_code is None
    assert version.worker_owner_id == "beta8-indexed-v1-acceptance"
    assert version.completed_at is None
    assert job.stage == "analyzing"
    assert job.error_code is None
    await database.dispose()


def test_paid_cli_routes_existing_output_through_resume_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_evaluation_script()
    resume_output = tmp_path / "run-existing"
    resume_output.mkdir()
    fresh_output = tmp_path / "must-not-create"
    current_plan = {
        "planned_output_directory": str(fresh_output),
        "input": {},
        "ground_truth": {},
        "prompt_binding": {},
        "expected_stages": {},
    }

    class ResumeSentinel(RuntimeError):
        pass

    async def stop_at_resume_preflight(*_args, **_kwargs):
        raise ResumeSentinel("resume preflight reached")

    monkeypatch.setattr(module, "prepare_dry_run", lambda _paths: current_plan)
    monkeypatch.setattr(module, "prepare_resume_paid_run", stop_at_resume_preflight)
    args = SimpleNamespace(
        run_paid=True,
        confirmed=True,
        resume_output=resume_output,
        report_provider="deepseek",
        report_model="deepseek-v4-pro",
        search_provider="kimi",
        search_model="kimi-k2.6",
    )
    paths = EvaluationPaths(
        transcript=Path("/unused"),
        manifest=Path("/unused"),
        ground_truth=Path("/unused"),
        output_root=tmp_path,
    )

    with pytest.raises(ResumeSentinel, match="resume preflight reached"):
        asyncio.run(module.run_paid(args, paths))

    assert not fresh_output.exists()
