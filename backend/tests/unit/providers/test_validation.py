from __future__ import annotations

import httpx
import pytest

from audio_memory.providers.types import ValidationErrorCode
from audio_memory.providers.validation import ProviderValidationService
from audio_memory.providers.adapters.openai import OpenAIAdapter
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
