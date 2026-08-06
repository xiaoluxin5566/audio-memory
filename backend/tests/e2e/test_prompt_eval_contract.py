from __future__ import annotations

import copy
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "prompt-eval"
EVALUATOR_PATH = PROJECT_ROOT / "scripts" / "evaluate-prompts.py"
FIXTURE_PATHS = tuple(sorted(FIXTURE_ROOT.glob("*.json")))

REQUIRED_COVERAGE = {
    "two_meetings",
    "one_event_multiple_scenes",
    "unrelated_content_events",
    "parenting_interactions",
    "other_person_todo",
    "media_call_to_action",
    "vague_title",
    "high_impact_growth_exception",
    "lightweight_inspiration_phrase",
    "prompt_injection",
    "overdue_todo",
    "multi_file_batch",
}


@pytest.fixture(scope="module")
def evaluator():
    spec = importlib.util.spec_from_file_location("prompt_evaluator", EVALUATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_saved_examples_pass_the_offline_release_contract(evaluator) -> None:
    report = evaluator.evaluate_fixtures(FIXTURE_PATHS)

    assert report.schema_valid_rate == 1.0
    assert report.unknown_evidence_ids == 0
    assert report.cross_event_contamination == 0
    assert report.false_user_todos == 0
    assert report.whisper_calls_during_reanalysis == 0
    assert report.overdue_auto_completions == 0
    assert report.secret_leaks == 0
    assert set(report.coverage) == REQUIRED_COVERAGE
    assert report.passed is True


def test_evaluator_detects_each_release_gate_regression(evaluator) -> None:
    payload = json.loads((FIXTURE_ROOT / "negative-cases.json").read_text())

    unknown = copy.deepcopy(payload)
    unknown["cases"][0]["scene_results"][0]["todos"][0][
        "evidence_segment_ids"
    ] = ["seg_missing"]
    assert evaluator.evaluate_fixture_data(unknown).unknown_evidence_ids == 1

    contaminated = copy.deepcopy(payload)
    contaminated["cases"][0]["scene_results"][0]["todos"][0][
        "evidence_segment_ids"
    ] = ["seg_media_cta"]
    assert evaluator.evaluate_fixture_data(contaminated).cross_event_contamination == 1

    false_todo = copy.deepcopy(payload)
    media_todo = copy.deepcopy(false_todo["cases"][0]["scene_results"][0]["todos"][0])
    media_todo.update(
        {
            "text": "点击关注",
            "action": "点击关注",
            "object": None,
            "source_event_id": "event_media_cta",
            "source_context": "媒体中的行动号召。",
            "evidence_segment_ids": ["seg_media_cta"],
        }
    )
    false_todo["cases"][0]["scene_results"][0]["todos"].append(media_todo)
    assert evaluator.evaluate_fixture_data(false_todo).false_user_todos == 1

    other_person = copy.deepcopy(payload)
    other_todo = other_person["cases"][0]["scene_results"][0]["todos"][0]
    other_todo.update(
        {
            "text": "小王发合同",
            "action": "发送",
            "object": "合同",
            "source_event_id": "event_other_todo",
            "source_context": "他人被指定发合同。",
            "evidence_segment_ids": ["seg_other_todo"],
        }
    )
    assert evaluator.evaluate_fixture_data(other_person).false_user_todos == 1

    unknown_event = copy.deepcopy(payload)
    unknown_event["cases"][0]["scene_results"][0]["todos"][0][
        "source_event_id"
    ] = "event_missing"
    assert evaluator.evaluate_fixture_data(unknown_event).passed is False

    reanalysis = copy.deepcopy(payload)
    reanalysis["cases"][0]["runtime_trace"]["whisper_calls"] = 1
    assert evaluator.evaluate_fixture_data(reanalysis).whisper_calls_during_reanalysis == 1

    overdue = copy.deepcopy(payload)
    overdue["cases"][0]["todo_states"][0]["status"] = "completed"
    assert evaluator.evaluate_fixture_data(overdue).overdue_auto_completions == 1

    leaked = copy.deepcopy(payload)
    leaked["cases"][0]["scene_results"][0]["todos"][0]["source_context"] = (
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    )
    assert evaluator.evaluate_fixture_data(leaked).secret_leaks == 1


def test_evaluator_rejects_incomplete_spoofed_or_inconsistent_fixtures(evaluator) -> None:
    payload = json.loads((FIXTURE_ROOT / "negative-cases.json").read_text())

    assert evaluator.evaluate_fixture_data([]).passed is False

    empty_spoof = {
        "fixture_version": 1,
        "coverage": sorted(REQUIRED_COVERAGE),
        "cases": [
            {
                "case_id": "empty-spoof",
                "event_map": {
                    "user_speaker": {
                        "speaker_id": None,
                        "confidence": 0,
                        "reasoning": "unknown",
                        "evidence_segment_ids": [],
                    },
                    "events": [],
                    "unassigned_segment_ids": [],
                },
            }
        ],
    }
    assert evaluator.evaluate_fixture_data(empty_spoof).passed is False

    missing_trace = copy.deepcopy(payload)
    missing_trace["cases"][0].pop("runtime_trace")
    assert evaluator.evaluate_fixture_data(missing_trace).passed is False

    duplicate_scene = copy.deepcopy(payload)
    duplicate_scene["cases"][0]["scene_results"][1] = copy.deepcopy(
        duplicate_scene["cases"][0]["scene_results"][0]
    )
    assert evaluator.evaluate_fixture_data(duplicate_scene).passed is False

    coverage_spoof = copy.deepcopy(payload)
    coverage_spoof["coverage"].append("prompt_injection")
    assert evaluator.evaluate_fixture_data(coverage_spoof).passed is False

    mismatched_user_evidence = copy.deepcopy(payload)
    mismatched_user_evidence["cases"][0]["event_map"]["user_speaker"][
        "evidence_segment_ids"
    ] = ["seg_other_todo"]
    mismatch_report = evaluator.evaluate_fixture_data(mismatched_user_evidence)
    assert mismatch_report.false_user_todos == 1
    assert mismatch_report.passed is False

    injected_speaker = copy.deepcopy(payload)
    other_event = next(
        event
        for event in injected_speaker["cases"][0]["event_map"]["events"]
        if event["event_id"] == "event_other_todo"
    )
    other_event["speaker_ids"].append("speaker_user")
    other_todo = injected_speaker["cases"][0]["scene_results"][0]["todos"][0]
    other_todo.update(
        {
            "source_event_id": "event_other_todo",
            "source_context": "他人被指定发合同。",
            "evidence_segment_ids": ["seg_other_todo"],
        }
    )
    assert evaluator.evaluate_fixture_data(injected_speaker).false_user_todos == 1

    unknown_content_event = copy.deepcopy(payload)
    content_card = unknown_content_event["cases"][0]["scene_results"][3]["cards"][0]
    content_card["event_ids"] = ["event_missing"]
    content_card["detail"]["consumed_items"][0]["event_id"] = "event_missing"
    assert evaluator.evaluate_fixture_data(unknown_content_event).passed is False


def test_cli_is_offline_only_and_does_not_accept_provider_execution() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EVALUATOR_PATH),
            "--provider",
            "deepseek",
            "--fixture",
            str(FIXTURE_ROOT / "multi-scene.json"),
        ],
        cwd=PROJECT_ROOT / "backend",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "offline-only" in result.stderr
    assert "deepseek" not in result.stdout.lower()


def test_doctor_exercises_phase_one_release_checks(tmp_path: Path) -> None:
    app_data = tmp_path / "Library" / "Application Support" / "AudioMemory"
    (app_data / "models" / "diarization" / "sherpa-onnx-pyannote-segmentation-3-0").mkdir(
        parents=True
    )
    (app_data / "whisper-model-manifest.json").write_text("{}")
    (app_data / "diarization-model-manifest.json").write_text("{}")
    (app_data / "models" / "diarization" / "silero_vad.onnx").write_bytes(b"vad")
    (
        app_data
        / "models"
        / "diarization"
        / "sherpa-onnx-pyannote-segmentation-3-0"
        / "model.int8.onnx"
    ).write_bytes(b"segmentation")
    (
        app_data
        / "models"
        / "diarization"
        / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
    ).write_bytes(b"embedding")
    database = app_data / "audio-memory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0007')")
        connection.execute("CREATE TABLE reanalysis_batches (status TEXT NOT NULL)")
        connection.execute("INSERT INTO reanalysis_batches VALUES ('paused')")

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "doctor.sh")],
        cwd=PROJECT_ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert "✓ Whisper 模型清单" in result.stdout
    assert "✓ 说话人分段模型" in result.stdout
    assert "✓ 分析迁移链" in result.stdout
    assert "✓ 历史重分析恢复" in result.stdout
    assert "✓ 本地会话安全" in result.stdout
    assert "✓ 本地数据库已迁移至 0007" in result.stdout
    assert "✓ 历史重分析状态已恢复" in result.stdout

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '0006'")
        connection.execute("INSERT INTO reanalysis_batches VALUES ('paused_error')")
    stale = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "doctor.sh")],
        cwd=PROJECT_ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "✗ 本地数据库已迁移至 0007" in stale.stdout
    assert "✗ 历史重分析状态已恢复" in stale.stdout
