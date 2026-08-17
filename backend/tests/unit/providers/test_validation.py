from __future__ import annotations

import httpx
import pytest

from audio_memory.providers.types import ValidationErrorCode
from audio_memory.providers.validation import ProviderValidationService
from audio_memory.providers.adapters.openai import OpenAIAdapter
from audio_memory.providers.adapters.deepseek import DeepSeekAdapter
from audio_memory.providers.adapters.kimi import KimiAdapter
from audio_memory.providers.types import PROVIDER_CONFIGS


@pytest.mark.asyncio
async def test_validation_requires_exact_normalized_ok(respx_mock) -> None:
    respx_mock.post("https://example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  oK\n"}}]},
        )
    )
    service = ProviderValidationService.for_test(
        provider_id="kimi",
        endpoint="https://example.test/v1/chat/completions",
        model_id="test-model",
    )

    result = await service.validate(b"secret")

    assert result.ok is True


@pytest.mark.asyncio
async def test_non_ok_model_text_is_protocol_error(respx_mock) -> None:
    respx_mock.post("https://example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Invalid API key"}}]},
        )
    )
    service = ProviderValidationService.for_test(
        provider_id="deepseek",
        endpoint="https://example.test/v1/chat/completions",
        model_id="test-model",
    )

    result = await service.validate(b"secret")

    assert result.ok is False
    assert result.error_code is ValidationErrorCode.VALIDATION_PROTOCOL_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ValidationErrorCode.INVALID_KEY),
        (403, ValidationErrorCode.PERMISSION_DENIED),
        (429, ValidationErrorCode.RATE_LIMITED),
        (503, ValidationErrorCode.PROVIDER_UNAVAILABLE),
    ],
)
async def test_http_errors_are_normalized(respx_mock, status, expected) -> None:
    respx_mock.post("https://example.test/v1/chat/completions").mock(
        return_value=httpx.Response(status, headers={"Retry-After": "12"})
    )
    service = ProviderValidationService.for_test(
        provider_id="openai",
        endpoint="https://example.test/v1/chat/completions",
        model_id="test-model",
    )

    result = await service.validate(b"secret")

    assert result.error_code is expected
    if status == 429:
        assert result.retry_after_seconds == 12


@pytest.mark.asyncio
async def test_openai_ip_allowlist_401_is_not_reported_as_invalid_key(
    respx_mock,
) -> None:
    respx_mock.post("https://example.test/v1/responses").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": "ip_not_authorized",
                    "message": "Your IP is not authorized to access this organization.",
                }
            },
        )
    )
    service = ProviderValidationService.for_test(
        provider_id="openai",
        endpoint="https://example.test/v1/responses",
        model_id="test-model",
    )

    result = await service.validate(b"secret")

    assert result.error_code is ValidationErrorCode.IP_NOT_AUTHORIZED
    assert result.message == "当前网络 IP 未获此 OpenAI 组织授权"


@pytest.mark.asyncio
async def test_openai_unknown_401_surfaces_only_its_safe_error_code(
    respx_mock,
) -> None:
    respx_mock.post("https://example.test/v1/responses").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": "organization_deactivated",
                    "message": "Do not expose this provider message.",
                }
            },
        )
    )
    service = ProviderValidationService.for_test(
        provider_id="openai",
        endpoint="https://example.test/v1/responses",
        model_id="test-model",
    )

    result = await service.validate(b"secret")

    assert result.error_code is ValidationErrorCode.OPENAI_AUTHENTICATION_REJECTED
    assert result.message == "OpenAI 拒绝了认证（原因代码：organization_deactivated）"


@pytest.mark.asyncio
async def test_kimi_401_surfaces_only_its_safe_error_code(respx_mock) -> None:
    respx_mock.post("https://example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": "invalid_api_key",
                    "message": "Do not expose this provider message.",
                }
            },
        )
    )
    service = ProviderValidationService.for_test(
        provider_id="kimi",
        endpoint="https://example.test/v1/chat/completions",
        model_id="test-model",
    )

    result = await service.validate(b"secret")

    assert result.error_code is ValidationErrorCode.PROVIDER_AUTHENTICATION_REJECTED
    assert result.message == "kimi 拒绝了认证（原因代码：invalid_api_key）"


@pytest.mark.asyncio
async def test_kimi_invalid_model_request_surfaces_safe_status_and_code(
    respx_mock,
) -> None:
    respx_mock.post("https://example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "model_not_found",
                    "message": "Do not expose this provider message.",
                }
            },
        )
    )
    service = ProviderValidationService.for_test(
        provider_id="kimi",
        endpoint="https://example.test/v1/chat/completions",
        model_id="test-model",
    )

    result = await service.validate(b"secret")

    assert result.error_code is ValidationErrorCode.PROVIDER_REQUEST_REJECTED
    assert result.message == "kimi 拒绝了校验请求（HTTP 400，原因代码：model_not_found）"


def test_openai_responses_adapter_uses_small_non_stored_request() -> None:
    adapter = OpenAIAdapter(PROVIDER_CONFIGS["openai"])

    payload = adapter.validation_payload()

    assert payload == {
        "model": "gpt-5-mini",
        "input": "Reply exactly: OK",
        "max_output_tokens": 4,
        "store": False,
    }
    assert adapter.extract_text({"output_text": "OK"}) == "OK"


def test_deepseek_validation_disables_thinking_for_short_protocol_response() -> None:
    adapter = DeepSeekAdapter(PROVIDER_CONFIGS["deepseek"])

    payload = adapter.validation_payload()

    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] >= 8


def test_kimi_k3_payloads_use_k3_reasoning_and_completion_contract() -> None:
    adapter = KimiAdapter(PROVIDER_CONFIGS["kimi"])
    payload = adapter.validation_payload()

    assert payload["model"] == "kimi-k3"
    assert "temperature" not in payload
    assert "thinking" not in payload
    assert payload["reasoning_effort"] == "low"
    assert payload["max_completion_tokens"] >= 64
    assert "max_tokens" not in payload

    analysis_payload = adapter.analysis_payload(
        {"model": "kimi-k3", "temperature": 0, "max_tokens": 32_768}
    )
    assert analysis_payload["reasoning_effort"] == "low"
    assert analysis_payload["max_completion_tokens"] == 32_768
    assert "temperature" not in analysis_payload
    assert "max_tokens" not in analysis_payload

    search_payload = adapter.native_search_payload(
        model_id="kimi-k3",
        messages=[{"role": "user", "content": "Search this"}],
        queries=["Search this"],
    )
    assert search_payload["reasoning_effort"] == "low"
    assert "thinking" not in search_payload
    assert "temperature" not in search_payload
