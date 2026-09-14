from __future__ import annotations

import json
from hashlib import sha256

import httpx
import pytest

from audio_memory.analysis.beta8_writing_transport import (
    SourceFetcher,
    WritingTransport,
    verify_source,
)
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class ConfiguredKeychain:
    def __init__(self, status: KeychainStatus = KeychainStatus.CONFIGURED) -> None:
        self.status = status
        self.reads: list[str] = []

    def read(self, provider_id: str) -> KeychainReadResult:
        self.reads.append(provider_id)
        secret = b"test-only-secret" if self.status is KeychainStatus.CONFIGURED else None
        return KeychainReadResult(self.status, secret)


def test_p5_is_a_supported_deepseek_report_stage() -> None:
    transport = WritingTransport(ConfiguredKeychain(), httpx.AsyncClient())
    transport._validate_route("P5", "deepseek", "deepseek-v4-pro", 48_000)


@pytest.mark.asyncio
async def test_transport_failure_keeps_exception_type_without_secret_or_resend():
    count = 0
    async def handle(request):
        nonlocal count
        count += 1
        raise httpx.RemoteProtocolError("sensitive detail test-only-secret", request=request)
    async def before(_payload):
        pass
    async def after(_body):
        pytest.fail("No complete response was returned")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderAnalysisError) as raised:
            await WritingTransport(ConfiguredKeychain(), client).complete(
                stage="P1", system="fixed", user="private input", provider_id="deepseek",
                model_id="deepseek-v4-pro", max_output_tokens=96000,
                before_request=before, after_response=after,
            )
    assert count == 1
    assert raised.value.code == "provider_unavailable"
    assert raised.value.transport_error_type == "RemoteProtocolError"
    assert "test-only-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_deepseek_stages_use_structured_json_and_report_the_complete_body() -> None:
    events: list[tuple[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert events == [("before", payload)]
        assert request.extensions["timeout"]["read"] == 900
        assert payload == {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "fixed P1"},
                {"role": "user", "content": '{"window":1}'},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 2048,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"ok":true}'},
                }],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    async def before(payload: dict[str, object]) -> None:
        events.append(("before", payload))

    async def after(body: object) -> None:
        events.append(("after", body))

    keychain = ConfiguredKeychain()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        raw = await WritingTransport(keychain, client).complete(
            stage="P1",
            system="fixed P1",
            user='{"window":1}',
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            max_output_tokens=2048,
            before_request=before,
            after_response=after,
        )

    assert raw == '{"ok":true}'
    assert keychain.reads == ["deepseek"]
    assert events[1][0] == "after"
    assert events[1][1]["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}
    assert events[1][1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "thinking", "effort"),
    [
        ("P4", {"type": "enabled"}, "high"),
        ("P5", {"type": "disabled"}, None),
    ],
)
async def test_writing_uses_high_reasoning_and_scoring_disables_it(
    stage: str, thinking: dict[str, str], effort: str | None,
) -> None:
    captured: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"ok":true}'},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    async def hook(_value: object) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        raw = await WritingTransport(ConfiguredKeychain(), client).complete(
            stage=stage, system=stage, user="{}", provider_id="deepseek",
            model_id="deepseek-v4-pro", max_output_tokens=2048,
            before_request=hook, after_response=hook,
        )

    assert raw == '{"ok":true}'
    assert captured[0]["thinking"] == thinking
    assert captured[0].get("reasoning_effort") == effort


@pytest.mark.asyncio
async def test_p3_preserves_supplied_prompt_and_native_tool_arguments() -> None:
    requests: list[dict[str, object]] = []
    responses: list[object] = []
    original_arguments = '{ "query": "official docs", "limit": 3 }'

    async def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.extensions["timeout"]["read"] == 120
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "tool-1",
                            "type": "builtin_function",
                            "function": {
                                "name": "$web_search",
                                "arguments": original_arguments,
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            })
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"status":"success"}'},
            }]
        })

    async def before(payload: dict[str, object]) -> None:
        assert len(requests) == len(responses)

    async def after(body: object) -> None:
        responses.append(body)

    keychain = ConfiguredKeychain()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        raw = await WritingTransport(keychain, client).complete(
            stage="P3",
            system="EXACT COMPILED P3 SYSTEM",
            user='{"task_key":"research-1"}',
            provider_id="kimi",
            model_id="kimi-k2.6",
            max_output_tokens=4096,
            before_request=before,
            after_response=after,
        )

    assert raw == '{"status":"success"}'
    assert len(requests) == 2
    assert len(responses) == 2
    assert keychain.reads == ["kimi"]
    assert "usage" not in responses[1]
    assert requests[0]["messages"] == [
        {"role": "system", "content": "EXACT COMPILED P3 SYSTEM"},
        {"role": "user", "content": '{"task_key":"research-1"}'},
    ]
    assert "exact four-line" not in json.dumps(requests[0], ensure_ascii=False)
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "tool-1",
        "name": "$web_search",
        "content": original_arguments,
    }


@pytest.mark.asyncio
async def test_budget_hook_stops_before_a_native_continuation_request() -> None:
    posts = 0
    hook_calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": f"tool-{posts}",
                        "type": "builtin_function",
                        "function": {"name": "$web_search", "arguments": "{}"},
                    }],
                },
            }]
        })

    async def before(payload: dict[str, object]) -> None:
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 2:
            raise RuntimeError("request budget exhausted")

    async def after(body: object) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError, match="request budget exhausted"):
            await WritingTransport(ConfiguredKeychain(), client).complete(
                stage="P3", system="P3", user="{}", provider_id="kimi",
                model_id="kimi-k2.6", max_output_tokens=100,
                before_request=before, after_response=after,
            )

    assert hook_calls == 2
    assert posts == 1


@pytest.mark.asyncio
async def test_unavailable_credential_makes_no_request_or_hook_call() -> None:
    hook_calls = 0
    posts = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(200, json={})

    async def hook(value: object) -> None:
        nonlocal hook_calls
        hook_calls += 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderAnalysisError) as raised:
            await WritingTransport(
                ConfiguredKeychain(KeychainStatus.UNAVAILABLE), client
            ).complete(
                stage="P1", system="P1", user="{}", provider_id="deepseek",
                model_id="deepseek-v4-pro", max_output_tokens=100,
                before_request=hook, after_response=hook,
            )

    assert raised.value.code == "keychain_unavailable"
    assert posts == 0
    assert hook_calls == 0


@pytest.mark.asyncio
async def test_non_200_is_not_retried_and_keeps_raw_body_out_of_error_text() -> None:
    posts = 0
    bodies: list[object] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(503, text="sensitive raw provider failure")

    async def before(payload: dict[str, object]) -> None:
        return None

    async def after(body: object) -> None:
        bodies.append(body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderAnalysisError) as raised:
            await WritingTransport(ConfiguredKeychain(), client).complete(
                stage="P2", system="P2", user="{}", provider_id="deepseek",
                model_id="deepseek-v4-pro", max_output_tokens=100,
                before_request=before, after_response=after,
            )

    assert posts == 1
    assert bodies == []
    assert raised.value.code == "provider_unavailable"
    assert raised.value.http_status_code == 503
    assert str(raised.value) == "Provider is temporarily unavailable"
    assert raised.value.partial_response == "sensitive raw provider failure"


@pytest.mark.asyncio
async def test_filtered_response_preserves_partial_text_for_the_caller() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "content_filter",
                "message": {"role": "assistant", "content": '{"partial":true}'},
            }]
        })

    async def hook(value: object) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderAnalysisError) as raised:
            await WritingTransport(ConfiguredKeychain(), client).complete(
                stage="P4", system="P4", user="{}", provider_id="deepseek",
                model_id="deepseek-v4-pro", max_output_tokens=100,
                before_request=hook, after_response=hook,
            )

    assert raised.value.code == "content_rejected"
    assert raised.value.partial_response == '{"partial":true}'


@pytest.mark.asyncio
async def test_malformed_http_body_keeps_status_and_raw_text() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{malformed provider JSON")

    responses: list[object] = []

    async def before(value: object) -> None:
        return None

    async def after(value: object) -> None:
        responses.append(value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderAnalysisError) as raised:
            await WritingTransport(ConfiguredKeychain(), client).complete(
                stage="P1", system="P1", user="{}", provider_id="deepseek",
                model_id="deepseek-v4-pro", max_output_tokens=100,
                before_request=before, after_response=after,
            )

    assert responses == []
    assert raised.value.code == "model_response_invalid"
    assert raised.value.http_status_code == 200
    assert raised.value.partial_response == "{malformed provider JSON"


@pytest.mark.asyncio
async def test_native_search_stops_after_eight_actual_responses() -> None:
    posts = 0
    responses = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": f"tool-{posts}",
                        "type": "builtin_function",
                        "function": {"name": "$web_search", "arguments": "{}"},
                    }],
                },
            }]
        })

    async def before(payload: dict[str, object]) -> None:
        return None

    async def after(body: object) -> None:
        nonlocal responses
        responses += 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderAnalysisError) as raised:
            await WritingTransport(ConfiguredKeychain(), client).complete(
                stage="P3", system="P3", user="{}", provider_id="kimi",
                model_id="kimi-k2.6", max_output_tokens=100,
                before_request=before, after_response=after,
            )

    assert raised.value.code == "native_search_round_limit"
    assert posts == 8
    assert responses == 8


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_source_fetcher_strips_active_html_and_hashes_meaningful_text() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=(
                "<html><head><style>.secret{display:none}</style>"
                "<script>steal()</script></head><body><h1>Official Guide</h1>"
                "<p>Use OAuth 2.0 for authorization.</p></body></html>"
            ),
        )

    fetcher = SourceFetcher(
        transport=httpx.MockTransport(handle), resolver=public_resolver, max_bytes=4096
    )
    result = await fetcher.fetch("https://example.com/guide")

    assert result["status"] == 200
    assert result["url"] == "https://example.com/guide"
    assert result["final_url"] == "https://example.com/guide"
    assert result["text"] == "Official Guide Use OAuth 2.0 for authorization."
    assert result["content_sha256"] == sha256(result["text"].encode()).hexdigest()
    assert result["fetched_at"].endswith("+00:00")
    assert result["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@example.com/private",
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://localhost/admin",
    ],
)
async def test_source_fetcher_rejects_unsafe_urls_without_a_request(url: str) -> None:
    posts = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(200, text="must not be reached")

    result = await SourceFetcher(
        transport=httpx.MockTransport(handle), resolver=public_resolver
    ).fetch(url)

    assert result["status"] is None
    assert result["error"] == "unsafe_url"
    assert result["safety_reason"] in {
        "invalid_url", "credentialed_url", "private_address", "local_host"
    }
    assert posts == 0


@pytest.mark.asyncio
async def test_source_fetcher_revalidates_redirect_and_rejects_private_target() -> None:
    requests: list[tuple[str, str]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), request.headers["host"]))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    result = await SourceFetcher(
        transport=httpx.MockTransport(handle), resolver=public_resolver
    ).fetch("https://example.com/start")

    assert requests == [("https://93.184.216.34/start", "example.com")]
    assert result["status"] == 302
    assert result["final_url"] == "http://169.254.169.254/latest"
    assert result["error"] == "unsafe_redirect"
    assert result["safety_reason"] == "private_address"


@pytest.mark.asyncio
async def test_source_fetcher_accepts_clash_fake_ip_only_for_a_validated_hostname() -> None:
    async def fake_ip_resolver(host: str) -> list[str]:
        assert host == "example.com"
        return ["198.18.12.34"]

    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "198.18.12.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="verified")

    result = await SourceFetcher(
        transport=httpx.MockTransport(handle), resolver=fake_ip_resolver
    ).fetch("https://example.com/source")

    assert result["error"] is None
    assert result["text"] == "verified"


@pytest.mark.asyncio
async def test_source_fetcher_rejects_direct_or_mixed_fake_ip_targets() -> None:
    requests = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, text="must not be reached")

    async def mixed_resolver(host: str) -> list[str]:
        return ["198.18.12.34", "127.0.0.1"]

    fetcher = SourceFetcher(
        transport=httpx.MockTransport(handle), resolver=mixed_resolver
    )
    direct = await fetcher.fetch("https://198.18.12.34/source")
    mixed = await fetcher.fetch("https://example.com/source")

    assert direct["safety_reason"] == "private_address"
    assert mixed["safety_reason"] == "private_address"
    assert requests == 0


def test_verify_source_requires_fetched_original_text_to_match() -> None:
    fetched = {
        "status": 200,
        "url": "https://example.com/guide",
        "final_url": "https://example.com/guide",
        "text": "The official guide says: Use OAuth 2.0 for authorization.",
        "fetched_at": "2026-09-09T01:02:03+00:00",
        "content_sha256": "abc",
        "error": None,
    }
    matched = verify_source(
        {
            "local_source_key": "source-1",
            "url": "https://example.com/guide",
            "access_level": "original_text",
            "quote": "Use   OAuth 2.0\nfor authorization.",
        },
        fetched,
    )
    mismatch = verify_source(
        {
            "local_source_key": "source-2",
            "url": "https://example.com/guide",
            "access_level": "original_text",
            "quote": "OAuth eliminates every security risk.",
        },
        fetched,
    )
    snippet = verify_source(
        {
            "local_source_key": "source-3",
            "url": "https://example.com/guide",
            "access_level": "search_snippet",
            "quote": "Use OAuth 2.0 for authorization.",
        },
        fetched,
    )
    wrong_page = verify_source(
        {
            "local_source_key": "source-4",
            "url": "https://attacker.example/claim",
            "access_level": "original_text",
            "quote": "Use OAuth 2.0 for authorization.",
        },
        fetched,
    )

    assert matched["quote_verified"] is True
    assert matched["verified_quote"] == "Use OAuth 2.0 for authorization."
    assert mismatch["quote_verified"] is False
    assert mismatch["verified_quote"] is None
    assert snippet["quote_verified"] is False
    assert snippet["verified_quote"] is None
    assert snippet["fetch"] == fetched
    assert wrong_page["quote_verified"] is False
