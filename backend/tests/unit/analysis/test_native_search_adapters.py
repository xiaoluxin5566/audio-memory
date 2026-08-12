from __future__ import annotations

import json

import httpx
import pytest

from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class ConfiguredKeychain:
    def read(self, provider_id: str) -> KeychainReadResult:
        return KeychainReadResult(KeychainStatus.CONFIGURED, b"test-only-secret")


@pytest.mark.asyncio
async def test_kimi_native_search_echoes_official_tool_call_and_normalizes_citations() -> None:
    requests: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_search_001",
                                        "type": "function",
                                        "function": {
                                            "name": "$web_search",
                                            "arguments": '{"query":"Kimi API web search"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Kimi documents the web-search tool.",
                            "citations": [
                                {
                                    "id": "kimi-web-search-docs",
                                    "title": "Use Web Search with the Kimi API",
                                    "url": "https://platform.kimi.com/docs/guide/use-web-search",
                                    "publisher": "Moonshot AI",
                                    "snippet": "Declare the built-in web search tool.",
                                }
                            ],
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            "kimi", queries=["Kimi API web search"], round_number=2
        )

    assert result.available is True
    assert result.provider_id == "kimi"
    assert result.model_id == "kimi-k2.5"
    assert result.tool_name == "$web_search"
    assert result.errors == ()
    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.provider_id == "kimi"
    assert source.provider_result_id == "kimi-web-search-docs"
    assert source.title == "Use Web Search with the Kimi API"
    assert source.url == "https://platform.kimi.com/docs/guide/use-web-search"
    assert source.search_round == 2
    assert requests[0]["tools"] == [
        {"type": "builtin_function", "function": {"name": "$web_search"}}
    ]
    assert requests[1]["messages"][:2] == requests[0]["messages"]
    tool_message = requests[1]["messages"][-1]
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "call_search_001",
        "name": "$web_search",
        "content": '{"query":"Kimi API web search"}',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["deepseek", "openai"])
async def test_unavailable_native_search_returns_structured_pure_audio_fallback(
    provider_id: str,
) -> None:
    async with httpx.AsyncClient() as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            provider_id, queries=["verify this claim"], round_number=1
        )

    assert result.available is False
    assert result.provider_id == provider_id
    assert result.model_id
    assert result.tool_name is None
    assert result.sources == ()
    assert result.errors == ("Native web search is not available for this configured provider.",)


@pytest.mark.asyncio
async def test_kimi_malformed_citation_is_returned_as_error_without_inventing_source() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_search_002",
                                        "type": "function",
                                        "function": {
                                            "name": "$web_search",
                                            "arguments": '{"query":"untrusted claim"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Unsupported citation.",
                            "citations": [
                                {
                                    "id": "not-a-real-source",
                                    "title": "Broken citation",
                                    "url": "not-a-url",
                                }
                            ],
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            "kimi", queries=["untrusted claim"], round_number=1
        )

    assert result.available is True
    assert result.sources == ()
    assert result.errors == ("Citation 0 is invalid: url must be an absolute HTTP(S) URL",)


@pytest.mark.asyncio
async def test_kimi_native_search_without_provider_citations_is_provenance_unavailable() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "No source envelope."},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            "kimi", queries=["source-free response"], round_number=1
        )

    assert result.available is False
    assert result.sources == ()
    assert result.errors == (
        "Native web search returned no provider-issued structured citations.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "content_filter", None])
async def test_kimi_native_search_rejects_non_stop_terminal_reasons(
    finish_reason: str | None,
) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        choice: dict[str, object] = {
            "message": {
                "role": "assistant",
                "content": "A response that must not be considered complete.",
                "citations": [
                    {
                        "id": "source_001",
                        "title": "Source",
                        "url": "https://example.com/source",
                    }
                ],
            }
        }
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return httpx.Response(200, json={"choices": [choice]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            "kimi", queries=["terminal state"], round_number=1
        )

    assert result.available is False
    assert result.sources == ()
    assert result.errors == ("Native web search did not complete normally.",)
