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
                    "usage": {"prompt_tokens": 120, "completion_tokens": 30},
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
                "usage": {"prompt_tokens": 180, "completion_tokens": 40},
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
    assert result.model_id == "kimi-k2.6"
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
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[1]["messages"][:2] == requests[0]["messages"]
    tool_message = requests[1]["messages"][-1]
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "call_search_001",
        "name": "$web_search",
        "content": '{"query":"Kimi API web search"}',
    }
    assert provider.native_search_usage_totals == {
        "input_tokens": 300,
        "output_tokens": 70,
        "token_usage_unavailable_reason": None,
        "response_count": 2,
        "tool_call_count": 1,
    }


@pytest.mark.asyncio
async def test_kimi_native_search_missing_usage_preserves_unknown_tokens_and_counts() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "call_search_missing_usage",
                                "type": "function",
                                "function": {
                                    "name": "$web_search",
                                    "arguments": '{"query":"missing usage"}',
                                },
                            }],
                        },
                    }],
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Result with a provider citation.",
                        "citations": [{
                            "id": "source-without-usage",
                            "title": "Provider source",
                            "url": "https://example.com/source",
                            "snippet": "Evidence.",
                        }],
                    },
                }],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            "kimi", queries=["missing usage"], round_number=1
        )

    assert result.available is True
    assert provider.native_search_usage_totals == {
        "input_tokens": None,
        "output_tokens": None,
        "token_usage_unavailable_reason": (
            "native search provider response omitted usage"
        ),
        "response_count": 2,
        "tool_call_count": 1,
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
async def test_kimi_k2_6_native_search_parses_sources_from_official_completion_content() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "t-web_search-001",
                                "type": "builtin_function",
                                "function": {
                                    "name": "$web_search",
                                    "arguments": json.dumps({
                                        "search_result": {"search_id": "search-001"},
                                        "usage": {"total_tokens": 8000},
                                    }),
                                },
                            }],
                        },
                    }],
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            "### 1. Kimi 联网搜索文档\n"
                            "- **ID**: `search-001`（搜索结果第1条）\n"
                            "- **标题**: 使用 Kimi API 的联网搜索功能\n"
                            "- **URL**: https://platform.kimi.com/docs/guide/use-web-search\n"
                            "- **说明**: 官方文档介绍 builtin_function.$web_search。\n"
                        ),
                    },
                }],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            "kimi",
            queries=["Kimi API 联网搜索官方文档"],
            round_number=1,
            model_id="kimi-k2.6",
        )

    assert result.available is True
    assert result.errors == ()
    assert len(result.sources) == 1
    assert result.sources[0].provider_result_id == "search-001:1"
    assert result.sources[0].title == "使用 Kimi API 的联网搜索功能"
    assert result.sources[0].url == "https://platform.kimi.com/docs/guide/use-web-search"
    assert result.sources[0].support_statement == (
        "官方文档介绍 builtin_function.$web_search。"
    )


def test_kimi_k2_6_parses_unbulleted_numbered_source_blocks() -> None:
    from audio_memory.providers.adapters.kimi import KimiAdapter
    from audio_memory.providers.types import PROVIDER_CONFIGS

    body = {
        "choices": [{
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": (
                    "**ID**: 1\n"
                    "**标题**: Kimi K2.6 快速入门\n"
                    "**URL**: https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart\n"
                    "**说明**: 官方文档说明工具参数兼容性。\n"
                ),
            },
        }],
    }

    citations = KimiAdapter(PROVIDER_CONFIGS["kimi"]).native_search_citations(body)

    assert citations == [{
        "id": "1",
        "title": "Kimi K2.6 快速入门",
        "url": "https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart",
        "snippet": "官方文档说明工具参数兼容性。",
    }]


@pytest.mark.asyncio
async def test_provider_unavailable_search_is_a_structured_retriable_result() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        result = await provider.native_search(
            "kimi", queries=["verify claim"], round_number=1
        )

    assert result.available is False
    assert result.retriable is True
    assert result.sources == ()
    assert result.errors == ("Native web search request returned HTTP 503.",)


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
