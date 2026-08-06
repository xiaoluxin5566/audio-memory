from __future__ import annotations

import copy
import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import httpx

from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus
from audio_memory.prompts.store import PromptStore


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


def test_user_todo_evidence_must_include_the_user_speaker(evaluator) -> None:
    payload = json.loads((FIXTURE_ROOT / "multi-scene.json").read_text())
    case = payload["cases"][0]
    case["transcript_segments"].append(
        {
            "segment_id": "seg_peer_scope",
            "file_id": "file_a",
            "file_name": "morning.m4a",
            "recording_started_at": "2026-08-01T09:00:00+08:00",
            "local_date": "2026-08-01",
            "timezone": "Asia/Shanghai",
            "start_ms": 4700,
            "end_ms": 5200,
            "speaker_id": "speaker_peer",
            "text": "你明天把范围文档发出去。",
        }
    )
    meeting_event = next(
        event
        for event in case["event_map"]["events"]
        if event["event_id"] == "event_meeting_a"
    )
    meeting_event["evidence_segment_ids"].append("seg_peer_scope")
    meeting_event["speaker_ids"].append("speaker_peer")
    case["scene_results"][0]["todos"][0]["evidence_segment_ids"] = [
        "seg_peer_scope"
    ]

    assert evaluator.evaluate_fixture_data(payload).false_user_todos == 1


def test_io_secrets_and_overdue_provenance_have_structured_gates(
    evaluator, tmp_path: Path
) -> None:
    invalid_utf8 = tmp_path / "invalid.json"
    invalid_utf8.write_bytes(b"\xff\xfe\x00")
    unreadable = evaluator.evaluate_fixtures([invalid_utf8])
    assert unreadable.passed is False
    assert unreadable.failures[0].code == "fixture_not_utf8"

    payload = json.loads((FIXTURE_ROOT / "negative-cases.json").read_text())
    transcript_leak = copy.deepcopy(payload)
    transcript_leak["cases"][0]["transcript_segments"][0]["text"] += (
        " sk-proj-transcriptsecret1234567890"
    )
    assert evaluator.evaluate_fixture_data(transcript_leak).secret_leaks == 1

    automatic = copy.deepcopy(payload)
    automatic_state = automatic["cases"][0]["todo_states"][0]
    automatic_state.update({"status": "completed", "completion_source": "model"})
    assert evaluator.evaluate_fixture_data(automatic).overdue_auto_completions == 1

    manual = copy.deepcopy(payload)
    manual_state = manual["cases"][0]["todo_states"][0]
    manual_state.update({"status": "completed", "completion_source": "user"})
    assert evaluator.evaluate_fixture_data(manual).overdue_auto_completions == 0


def test_coverage_is_type_and_evidence_aware(evaluator) -> None:
    multi = json.loads((FIXTURE_ROOT / "multi-scene.json").read_text())
    segment_ids = {
        segment["segment_id"] for segment in multi["cases"][0]["transcript_segments"]
    }
    assert {"seg_m1_feedback", "seg_c1_user"}.issubset(segment_ids)
    multi_report = evaluator.evaluate_fixture_data(multi)
    assert not multi_report.errors
    assert set(multi_report.coverage) == set(multi["coverage"])

    wrong_meeting_type = copy.deepcopy(multi)
    next(
        event
        for event in wrong_meeting_type["cases"][0]["event_map"]["events"]
        if event["event_id"] == "event_meeting_b"
    )["event_type"] = "casual_chat"
    assert "two_meetings" not in evaluator.evaluate_fixture_data(
        wrong_meeting_type
    ).coverage

    related_content = copy.deepcopy(multi)
    events = related_content["cases"][0]["event_map"]["events"]
    video_topics = next(
        event["topics"] for event in events if event["event_id"] == "event_content_video"
    )
    next(
        event for event in events if event["event_id"] == "event_content_launch"
    )["topics"] = video_topics
    assert "unrelated_content_events" not in evaluator.evaluate_fixture_data(
        related_content
    ).coverage

    wrong_parenting_type = copy.deepcopy(multi)
    next(
        event
        for event in wrong_parenting_type["cases"][0]["event_map"]["events"]
        if event["event_id"] == "event_parenting_a"
    )["event_type"] = "other"
    assert "parenting_interactions" not in evaluator.evaluate_fixture_data(
        wrong_parenting_type
    ).coverage

    unsupported_growth = copy.deepcopy(multi)
    growth_case = unsupported_growth["cases"][0]["scene_results"][4]["cards"][0][
        "detail"
    ]["directions"][0]["cases"][0]
    growth_case["evidence_segment_ids"] = ["seg_m1"]
    assert "high_impact_growth_exception" not in evaluator.evaluate_fixture_data(
        unsupported_growth
    ).coverage

    unsupported_inspiration = copy.deepcopy(multi)
    inspiration_idea = unsupported_inspiration["cases"][0]["scene_results"][5][
        "cards"
    ][0]["detail"]["ideas"][0]
    inspiration_idea["evidence_segment_ids"] = ["seg_c1"]
    next(
        event
        for event in unsupported_inspiration["cases"][0]["event_map"]["events"]
        if event["event_id"] == "event_meeting_a"
    )["candidate_scenes"] = ["todo"]
    assert "one_event_multiple_scenes" not in evaluator.evaluate_fixture_data(
        unsupported_inspiration
    ).coverage

    cross_event_inspiration = copy.deepcopy(multi)
    cross_case = cross_event_inspiration["cases"][0]
    cross_events = {
        event["event_id"]: event for event in cross_case["event_map"]["events"]
    }
    cross_events["event_meeting_a"]["candidate_scenes"] = ["todo"]
    cross_events["event_content_video"]["candidate_scenes"] = ["content"]
    cross_events["event_content_launch"]["candidate_scenes"].append("inspiration")
    inspiration_card = cross_case["scene_results"][5]["cards"][0]
    media_only_idea = copy.deepcopy(inspiration_card["detail"]["ideas"][0])
    media_only_idea.update(
        {
            "event_id": "event_content_launch",
            "start_ms": 2200,
            "end_ms": 3200,
            "evidence_segment_ids": ["seg_c2"],
        }
    )
    inspiration_card["event_ids"] = [
        "event_content_video",
        "event_content_launch",
    ]
    inspiration_card["detail"]["ideas"].append(media_only_idea)
    assert "one_event_multiple_scenes" not in evaluator.evaluate_fixture_data(
        cross_event_inspiration
    ).coverage

    negative = json.loads((FIXTURE_ROOT / "negative-cases.json").read_text())
    wrong_other_type = copy.deepcopy(negative)
    next(
        event
        for event in wrong_other_type["cases"][0]["event_map"]["events"]
        if event["event_id"] == "event_other_todo"
    )["event_type"] = "video"
    assert "other_person_todo" not in evaluator.evaluate_fixture_data(
        wrong_other_type
    ).coverage

    no_due_provenance = copy.deepcopy(negative)
    no_due_provenance["cases"][0]["scene_results"][0]["todos"][0]["due_at"] = None
    assert "overdue_todo" not in evaluator.evaluate_fixture_data(
        no_due_provenance
    ).coverage

    unsupported_light_phrase = copy.deepcopy(negative)
    next(
        event
        for event in unsupported_light_phrase["cases"][0]["event_map"]["events"]
        if event["event_id"] == "event_light_phrase"
    )["event_type"] = "media"
    assert "lightweight_inspiration_phrase" not in evaluator.evaluate_fixture_data(
        unsupported_light_phrase
    ).coverage

    injection = json.loads((FIXTURE_ROOT / "injection.json").read_text())
    unassigned_injection = copy.deepcopy(injection)
    injection_case = unassigned_injection["cases"][0]
    injection_case["event_map"]["events"] = []
    injection_case["event_map"]["unassigned_segment_ids"] = ["seg_injection"]
    assert "prompt_injection" not in evaluator.evaluate_fixture_data(
        unassigned_injection
    ).coverage


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


def test_provider_mode_uses_fake_backend_and_writes_only_redacted_report(
    evaluator, tmp_path: Path
) -> None:
    secret = "sk-proj-provider-secret-abcdefghijklmnopqrstuvwxyz"

    class FakeProviderBackend:
        async def run_case(self, provider_id, case):
            assert provider_id == "deepseek"
            return evaluator.ProviderCaseOutput(
                event_map=case.event_map,
                scene_results=case.scene_results,
                model_id="fake-model",
                prompt_versions={scene: 7 for scene in evaluator.PROMPT_SCENES},
                latency_ms=12,
                token_usage={"input_tokens": 100, "output_tokens": 20},
            )

    result = asyncio.run(
        evaluator.run_provider_evaluation(
            "deepseek",
            FIXTURE_PATHS,
            backend=FakeProviderBackend(),
            report_root=tmp_path,
        )
    )

    assert result.report.passed is True
    report_payload = json.loads(result.report_path.read_text())
    assert report_payload["mode"] == "provider"
    assert report_payload["provider"] == "deepseek"
    assert report_payload["model_id"] == "fake-model"
    assert report_payload["prompt_versions"]["todo"] == 7
    assert report_payload["latency_ms"] == 36
    assert report_payload["token_usage"] == {
        "input_tokens": 300,
        "output_tokens": 60,
    }
    serialized = result.report_path.read_text()
    assert secret not in serialized
    assert "transcript_segments" not in serialized
    assert "fixture" not in serialized.lower()
    assert result.report_path.stat().st_mode & 0o777 == 0o600
    assert result.report_path.parent.stat().st_mode & 0o777 == 0o700

    partial = asyncio.run(
        evaluator.run_provider_evaluation(
            "deepseek",
            [FIXTURE_ROOT / "multi-scene.json"],
            backend=FakeProviderBackend(),
            report_root=tmp_path / "partial",
        )
    )
    assert partial.report.passed is False
    assert partial.passed is True
    assert json.loads(partial.report_path.read_text())["passed"] is True


def test_provider_cli_dispatches_only_when_explicitly_selected(
    evaluator, monkeypatch, capsys
) -> None:
    observed = {}

    async def fake_provider_cli(provider_id, fixture_paths, report_root):
        observed.update(
            provider=provider_id,
            fixtures=list(fixture_paths),
            report_root=report_root,
        )
        return 0

    monkeypatch.setattr(evaluator, "_run_provider_cli", fake_provider_cli)
    result = evaluator.main(
        [
            "--provider",
            "openai",
            "--fixture",
            str(FIXTURE_ROOT / "multi-scene.json"),
        ]
    )

    assert result == 0
    assert observed["provider"] == "openai"
    assert "openai" not in capsys.readouterr().out.lower()


def test_provider_failure_report_does_not_persist_sensitive_input(
    evaluator, tmp_path: Path
) -> None:
    secret = "sk-proj-provider-secret-abcdefghijklmnopqrstuvwxyz"
    payload = json.loads((FIXTURE_ROOT / "negative-cases.json").read_text())
    payload["cases"][0]["transcript_segments"][0]["text"] += f" {secret}"
    fixture = tmp_path / "sensitive-input.json"
    fixture.write_text(json.dumps(payload))

    class EchoFixtureBackend:
        async def run_case(self, provider_id, case):
            return evaluator.ProviderCaseOutput(
                event_map=case.event_map,
                scene_results=case.scene_results,
                model_id="fake-model",
                prompt_versions={scene: 1 for scene in evaluator.PROMPT_SCENES},
                latency_ms=1,
            )

    result = asyncio.run(
        evaluator.run_provider_evaluation(
            "kimi",
            [fixture],
            backend=EchoFixtureBackend(),
            report_root=tmp_path / "reports",
        )
    )

    serialized = result.report_path.read_text()
    assert result.report.secret_leaks == 1
    assert result.report.passed is False
    assert secret not in serialized
    assert "transcript_segments" not in serialized


def test_provider_output_failure_is_sanitized_and_still_writes_private_report(
    evaluator, tmp_path: Path
) -> None:
    secret = "sk-proj-generated-secret-abcdefghijklmnopqrstuvwxyz"

    class SecretFailureBackend:
        async def run_case(self, provider_id, case):
            raise ValueError(f"invalid generated output: {secret} from /private/input.json")

    result = asyncio.run(
        evaluator.run_provider_evaluation(
            "openai",
            [FIXTURE_ROOT / "multi-scene.json"],
            backend=SecretFailureBackend(),
            report_root=tmp_path / "reports",
        )
    )

    serialized = result.report_path.read_text()
    assert result.passed is False
    assert result.report.failures[0].code == "provider_case_failed"
    assert secret not in serialized
    assert "/private/input.json" not in serialized
    assert result.report_path.stat().st_mode & 0o777 == 0o600
    assert result.report_path.parent.stat().st_mode & 0o777 == 0o700


def test_real_provider_path_uses_injected_keychain_and_mock_transport_only(
    evaluator, tmp_path: Path
) -> None:
    payload = json.loads((FIXTURE_ROOT / "multi-scene.json").read_text())
    case = evaluator._EvaluationCase.model_validate(payload["cases"][0])
    responses = iter(
        [
            case.event_map.model_dump(mode="json"),
            *[
                result.model_dump(mode="json")
                for result in case.scene_results
            ],
        ]
    )
    requests: list[httpx.Request] = []

    class FakeKeychain:
        def __init__(self) -> None:
            self.reads = 0

        def read(self, provider_id):
            self.reads += 1
            assert provider_id == "kimi"
            return KeychainReadResult(KeychainStatus.CONFIGURED, b"test-only-key")

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(next(responses))}}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    keychain = FakeKeychain()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ) as client:
            backend = evaluator._RealProviderBackend(
                keychain=keychain,
                client=client,
                prompt_store=PromptStore(tmp_path / "prompts"),
            )
            return await backend.run_case("kimi", case)

    output = asyncio.run(run())

    assert len(requests) == 7
    assert keychain.reads == 7
    assert all(
        request.headers["authorization"] == "Bearer test-only-key"
        for request in requests
    )
    assert output.event_map == case.event_map
    assert output.scene_results == case.scene_results
    assert output.token_usage == {"input_tokens": 14, "output_tokens": 7}


def test_doctor_exercises_phase_one_release_checks(tmp_path: Path) -> None:
    app_data = tmp_path / "Library" / "Application Support" / "AudioMemory"
    diarization_root = app_data / "models" / "diarization"
    (diarization_root / "sherpa-onnx-pyannote-segmentation-3-0").mkdir(parents=True)
    snapshot = tmp_path / "whisper-snapshot"
    snapshot.mkdir()
    config_file = snapshot / "config.json"
    config_file.write_text(json.dumps({"model_type": "whisper"}))
    whisper_file = snapshot / "weights.safetensors"
    whisper_file.write_bytes(b"real-whisper-fixture")
    (app_data / "whisper-model-manifest.json").write_text(
        json.dumps(
            {
                "model_id": "mlx-community/whisper-large-v3-turbo",
                "snapshot": str(snapshot),
                "files": [
                    {
                        "path": "config.json",
                        "size": config_file.stat().st_size,
                        "sha256": hashlib.sha256(
                            config_file.read_bytes()
                        ).hexdigest(),
                    },
                    {
                        "path": "weights.safetensors",
                        "size": whisper_file.stat().st_size,
                        "sha256": hashlib.sha256(whisper_file.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
    )
    diarization_files = {
        "models/diarization/silero_vad.onnx": b"trusted vad\n",
        "models/diarization/sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx": b"trusted segmentation\n",
        "models/diarization/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx": b"trusted embedding\n",
    }
    manifest_files = []
    for relative_path, content in diarization_files.items():
        model_path = app_data / relative_path
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        manifest_files.append(
            {
                "path": relative_path,
                "sha256": digest,
                "expected_sha256": digest,
                "size": len(content),
                "expected_size": len(content),
            }
        )
    (app_data / "diarization-model-manifest.json").write_text(
        json.dumps({"files": manifest_files})
    )
    database = app_data / "audio-memory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0007')")
        connection.execute(
            "CREATE TABLE reanalysis_batches (id TEXT, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO reanalysis_batches VALUES ('b1', 'paused')")
        connection.execute(
            "CREATE TABLE analysis_versions (id TEXT, status TEXT, worker_owner_id TEXT, lease_expires_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE reanalysis_items (analysis_version_id TEXT, status TEXT, completed_at TEXT)"
        )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "doctor.sh")],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "AUDIO_MEMORY_DOCTOR_CORE_ONLY": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "✓ Whisper 模型清单" in result.stdout
    assert "✗ 说话人分段模型" in result.stdout
    assert "✓ 分析迁移链" in result.stdout
    assert "✓ 历史重分析恢复" in result.stdout
    assert "✓ 本地会话安全" in result.stdout
    assert "✓ 本地数据库已迁移至 0007" in result.stdout
    assert "✓ 历史重分析状态已恢复" in result.stdout
    vad_path = app_data / "models/diarization/silero_vad.onnx"
    trusted_vad = vad_path.read_bytes()
    vad_path.write_bytes(b"bad")
    bogus_model = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "doctor_checks.py"),
            "diarization",
            str(app_data),
        ],
        check=False,
    )
    assert bogus_model.returncode == 1
    vad_path.write_bytes(trusted_vad)

    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO alembic_version VALUES ('bogus-head')")
    multiple_heads = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "doctor_checks.py"),
            "database",
            str(database),
        ],
        check=False,
    )
    assert multiple_heads.returncode == 1
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM alembic_version WHERE version_num = 'bogus-head'"
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO reanalysis_items VALUES (NULL, 'running', NULL)"
        )
    invalid_recovery = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "doctor_checks.py"),
            "recovery",
            str(database),
        ],
        check=False,
    )
    assert invalid_recovery.returncode == 1
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM reanalysis_items")

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '0006'")
        connection.execute("INSERT INTO reanalysis_batches VALUES ('b2', 'paused_error')")
    stale = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "doctor.sh")],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "AUDIO_MEMORY_DOCTOR_CORE_ONLY": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert "✗ 本地数据库已迁移至 0007" in stale.stdout
    assert "✗ 历史重分析状态已恢复" in stale.stdout
    assert stale.returncode == 1

    (app_data / "whisper-model-manifest.json").write_text("{}")
    invalid_model = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "doctor.sh")],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "AUDIO_MEMORY_DOCTOR_CORE_ONLY": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert "✗ Whisper 模型清单" in invalid_model.stdout
    assert invalid_model.returncode == 1


def test_doctor_accepts_huggingface_style_symlinked_whisper_snapshot(
    tmp_path: Path,
) -> None:
    app_data = tmp_path / "app-data"
    snapshot = tmp_path / "hub" / "snapshots" / "revision"
    blobs = tmp_path / "hub" / "blobs"
    snapshot.mkdir(parents=True)
    blobs.mkdir()
    config_blob = blobs / "config"
    config_blob.write_text(json.dumps({"model_type": "whisper"}))
    weights_blob = blobs / "weights"
    weights_blob.write_bytes(b"trusted weights")
    (snapshot / "config.json").symlink_to(config_blob)
    (snapshot / "weights.safetensors").symlink_to(weights_blob)
    app_data.mkdir()
    app_data.joinpath("whisper-model-manifest.json").write_text(
        json.dumps(
            {
                "model_id": "mlx-community/whisper-large-v3-turbo",
                "snapshot": str(snapshot),
                "files": [
                    {
                        "path": "config.json",
                        "size": config_blob.stat().st_size,
                        "sha256": hashlib.sha256(config_blob.read_bytes()).hexdigest(),
                    },
                    {
                        "path": "weights.safetensors",
                        "size": weights_blob.stat().st_size,
                        "sha256": hashlib.sha256(weights_blob.read_bytes()).hexdigest(),
                    },
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "doctor_checks.py"),
            "whisper",
            str(app_data),
        ],
        check=False,
    )

    assert result.returncode == 0


def test_doctor_rejects_tampered_migration_chain(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    for source in (PROJECT_ROOT / "backend" / "migrations" / "versions").glob("*.py"):
        target = versions / source.name
        text = source.read_text()
        if source.name.startswith("0007_"):
            text = text.replace('down_revision: Union[str, Sequence[str], None] = "0006"', 'down_revision = "0005"')
        target.write_text(text)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "doctor_checks.py"),
            "migrations",
            str(versions),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
