from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from audio_memory.analysis import provider as provider_module
from audio_memory.analysis import windows as windows_module
from audio_memory.analysis.provider import (
    ProviderAnalysisClient,
    ProviderAnalysisError,
    RemoteSceneAnalyzer,
)
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.director_schema import DirectorResult
from audio_memory.prompts.event_schema import EventMap
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class ConfiguredKeychain:
    def read(self, provider_id: str) -> KeychainReadResult:
        return KeychainReadResult(KeychainStatus.CONFIGURED, b"test-only-secret")


class DirectorClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        self.calls.append({"provider_id": provider_id, **kwargs})
        return json.dumps(
            {
                "selections": [
                    {
                        "selection_id": "selection_001",
                        "cluster_ids": ["cluster_1234567890abcdefabcd"],
                        "source_event_ids": [],
                        "candidate_scenes": ["meeting"],
                        "title": "Synthetic work discussion",
                        "selection_reason": "Contains a bounded work decision.",
                        "value_signals": ["explicit_decision"],
                        "priority": "high",
                        "context_before_clusters": 0,
                        "context_after_clusters": 0,
                    }
                ]
            }
        )


class StructuredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str


class StructuredClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        self.calls.append({"provider_id": provider_id, **kwargs})
        return self.responses.pop(0)


def test_deepseek_parameter_fingerprint_includes_analysis_window_policy(
    monkeypatch,
) -> None:
    baseline = provider_module._analysis_parameter_fingerprint()

    monkeypatch.setattr(
        windows_module,
        "ANALYSIS_WINDOW_GAP_MS",
        windows_module.ANALYSIS_WINDOW_GAP_MS + 1,
    )

    changed = provider_module._analysis_parameter_fingerprint()
    assert changed != baseline
    assert "PRIVATE_FIXTURE_TEXT" not in changed


@pytest.mark.asyncio
async def test_provider_analysis_client_registers_glm_adapter() -> None:
    async with httpx.AsyncClient() as http_client:
        client = ProviderAnalysisClient(ConfiguredKeychain(), http_client)

    assert "glm" in client.adapters


@pytest.mark.asyncio
async def test_provider_can_run_explicit_parallel_audit_calls(monkeypatch) -> None:
    async with httpx.AsyncClient() as http_client:
        client = ProviderAnalysisClient(ConfiguredKeychain(), http_client)
        active = 0
        max_active = 0

        async def fake_generate(*args: object, **kwargs: object) -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return str(kwargs["scene_id"])

        monkeypatch.setattr(client, "_generate_serialized", fake_generate)
        results = await asyncio.gather(
            client.generate(
                "deepseek", system="rules", user="chunk 1",
                scene_id="audit-chunk-1", allow_parallel=True,
            ),
            client.generate(
                "deepseek", system="rules", user="chunk 2",
                scene_id="audit-chunk-2", allow_parallel=True,
            ),
        )

    assert results == ["audit-chunk-1", "audit-chunk-2"]
    assert max_active == 2


def transcript() -> list[dict[str, object]]:
    return [
        {
            "segment_id": "seg_0_0",
            "file_id": "file-1",
            "file_name": "fixture.mp3",
            "recording_started_at": None,
            "local_date": None,
            "timezone": None,
            "start_ms": 0,
            "end_ms": 1_000,
            "speaker_id": "unknown",
            "text": "PRIVATE_FIXTURE_TEXT",
            "reliability_weight": 1.0,
        }
    ]


@pytest.mark.asyncio
async def test_remote_analyzer_parses_director_result_through_strict_boundary() -> None:
    client = DirectorClient()
    analyzer = RemoteSceneAnalyzer(client)
    request = type(
        "Request",
        (),
        {
            "rendered_instructions": "rules",
            "user_data": "data",
            "scene_id": "director:cluster_1234567890abcdefabcd",
            "max_tokens": 16_384,
            "timeout_seconds": 120,
            "segment_count": 1,
            "schema_json": json.dumps(DirectorResult.model_json_schema()),
        },
    )()

    result = await analyzer.analyze_director(
        request,
        {"provider_id": "deepseek", "model_id": "deepseek-v4-flash"},
    )

    assert isinstance(result, DirectorResult)
    assert result.selections[0].candidate_scenes == ["meeting"]
    assert client.calls[0]["scene_id"] == request.scene_id
    assert client.calls[0]["repair_attempted"] is False


@pytest.mark.asyncio
async def test_remote_analyzer_runs_any_new_phase_through_strict_schema() -> None:
    client = StructuredClient(['{"status":"ready"}'])
    analyzer = RemoteSceneAnalyzer(client)
    request = type(
        "Request",
        (),
        {
            "rendered_instructions": "rules",
            "user_data": "data",
            "scene_id": "writing-prepare",
            "max_tokens": 8_192,
            "timeout_seconds": 180,
            "segment_count": 0,
        },
    )()

    result = await analyzer.analyze_structured(
        request,
        {"provider_id": "deepseek", "model_id": "deepseek-v4-pro"},
        result_type=StructuredResult,
        invalid_code="writing_prepare_invalid",
    )

    assert result == StructuredResult(status="ready")
    assert client.calls[0]["repair_attempted"] is False


@pytest.mark.asyncio
async def test_remote_analyzer_repairs_invalid_new_phase_only_once() -> None:
    client = StructuredClient(["{}", '{"status":"ready"}'])
    analyzer = RemoteSceneAnalyzer(client)
    request = type(
        "Request",
        (),
        {
            "rendered_instructions": "rules",
            "user_data": "data",
            "scene_id": "writer-session",
            "max_tokens": 32_768,
            "timeout_seconds": 300,
            "segment_count": 0,
        },
    )()

    result = await analyzer.analyze_structured(
        request,
        {"provider_id": "deepseek", "model_id": "deepseek-v4-pro"},
        result_type=StructuredResult,
        invalid_code="writer_session_invalid",
    )

    assert result.status == "ready"
    assert [call["repair_attempted"] for call in client.calls] == [False, True]
    assert "validation_feedback" in str(client.calls[1]["user"])


@pytest.mark.asyncio
async def test_deepseek_request_is_bounded_and_enables_thinking() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "{}"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )

    request = PromptComposer().compose_event_map(
        transcript=transcript(), profile=[], schema=EventMap.model_json_schema()
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.generate(
            "deepseek",
            system=request.rendered_instructions,
            user=request.user_data,
            model_id="deepseek-v4-pro",
            scene_id=request.scene_id,
            max_tokens=request.max_tokens,
            timeout_seconds=request.timeout_seconds,
            segment_count=request.segment_count,
        )

    assert result == "{}"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["max_tokens"] == 32_768
    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}
    timeout = captured["timeout"]
    assert isinstance(timeout, dict)
    assert timeout["read"] == 180.0
    assert provider.usage_totals == {"input_tokens": 9, "output_tokens": 4}
    assert len(provider.parameter_fingerprint) == 64


@pytest.mark.asyncio
async def test_deepseek_markdown_request_uses_text_mode_and_reasoning_effort() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# 今日报告"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.generate_markdown(
            "deepseek",
            system="system rules",
            user="full transcript",
            model_id="deepseek-v4-pro",
            max_tokens=32_768,
            timeout_seconds=900,
            segment_count=10,
        )

    assert result == "# 今日报告"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert payload["response_format"] == {"type": "text"}
    assert "temperature" not in payload


@pytest.mark.asyncio
async def test_deepseek_scan_can_explicitly_disable_thinking() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        await provider.generate(
            "deepseek",
            system="Return JSON.",
            user="scan window",
            scene_id="historical-scan",
            max_tokens=4_096,
            thinking_enabled=False,
        )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_deepseek_length_finish_reason_is_typed_and_diagnostic_is_content_free(
    monkeypatch,
) -> None:
    logged: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        provider_module.logger, "info", lambda *values: logged.append(values)
    )
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "PRIVATE_RESPONSE_TEXT"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        with pytest.raises(ProviderAnalysisError) as raised:
            await provider.generate(
                "deepseek",
                system="PRIVATE_SYSTEM_TEXT",
                user="PRIVATE_FIXTURE_TEXT",
                scene_id="event-map",
                max_tokens=32_768,
                timeout_seconds=180,
                segment_count=3_442,
            )

    assert raised.value.code == "model_output_truncated"
    assert len(provider.request_diagnostics) == 1
    diagnostic = provider.request_diagnostics[0]
    assert diagnostic.scene_id == "event-map"
    assert diagnostic.finish_reason == "length"
    assert diagnostic.segment_count == 3_442
    assert diagnostic.input_tokens == 10
    assert diagnostic.output_tokens == 5
    assert diagnostic.request_bytes > 0
    assert diagnostic.response_bytes > 0
    serialized = repr(diagnostic)
    assert "PRIVATE_SYSTEM_TEXT" not in serialized
    assert "PRIVATE_FIXTURE_TEXT" not in serialized
    assert "PRIVATE_RESPONSE_TEXT" not in serialized
    assert "test-only-secret" not in serialized
    assert provider_module.logger.name == "uvicorn.error"
    logged_repr = repr(logged)
    assert "analysis_provider_request" in logged_repr
    assert "event-map" in logged_repr
    assert "PRIVATE_SYSTEM_TEXT" not in logged_repr
    assert "PRIVATE_FIXTURE_TEXT" not in logged_repr
    assert "PRIVATE_RESPONSE_TEXT" not in logged_repr
    assert "test-only-secret" not in logged_repr


@pytest.mark.asyncio
async def test_transient_provider_failure_gets_two_extra_attempts() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        with pytest.raises(ProviderAnalysisError) as raised:
            await provider.generate(
                "deepseek",
                system="rules",
                user="data",
                scene_id="meeting",
                max_tokens=16_384,
                timeout_seconds=120,
            )

    assert raised.value.code == "provider_unavailable"
    assert calls == 3


@pytest.mark.asyncio
async def test_transient_provider_failure_can_recover_on_third_attempt() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "recovered"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.generate_markdown(
            "deepseek",
            system="rules",
            user="data",
            scene_id="direct-report",
            max_tokens=16_384,
            timeout_seconds=120,
        )

    assert result == "recovered"
    assert calls == 3


@pytest.mark.asyncio
async def test_incomplete_provider_response_is_retried() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError(
                "incomplete chunked read", request=request
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "recovered"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.generate_markdown(
            "deepseek",
            system="rules",
            user="data",
            scene_id="direct-report",
            max_tokens=16_384,
            timeout_seconds=120,
        )

    assert result == "recovered"
    assert calls == 2
