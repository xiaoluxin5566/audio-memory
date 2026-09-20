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
async def test_p3_calls_search_pro_once_and_returns_retrieved_page_chunks() -> None:
    requests: list[dict[str, object]] = []
    responses: list[object] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.extensions["timeout"]["read"] == 120
        assert str(request.url) == "https://api.moonshot.cn/v1/tools/search_pro"
        requests.append(payload)
        return httpx.Response(200, json={
            "search_results": [{
                "title": "官方开放指南",
                "url": "https://example.com/guide?utm_source=test",
                "site_name": "官方网站",
                "date": "2026-09-19",
                "snippet": "预约与开放提示",
                "chunks": [
                    {"text": "周一至周日 09:00–18:00 开放，需提前预约。", "score": 1.23},
                    {"text": "儿童需由成人陪同入场。", "score": 1.01},
                ],
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
            research_task={
                "task_key": "research-1",
                "question": "明天开放吗？",
                "purpose": "安排亲子行程",
                "public_context": "阿那亚",
                "source_requirements": "官方页面",
                "as_of": "2026-09-20",
                "stop_condition": "确认开放时间和预约条件",
                "source_limit": 3,
            },
        )

    value = json.loads(raw)
    assert len(requests) == 1
    assert len(responses) == 1
    assert keychain.reads == ["kimi"]
    assert requests[0] == {
        "text_query": (
            "阿那亚 明天开放吗？ 用于安排亲子行程。"
            "条件：官方页面。截止日期：2026-09-20。"
            "需满足：确认开放时间和预约条件。"
        ),
        "limit": 3,
        "timeout_seconds": 60,
    }
    assert value["task_key"] == "research-1"
    assert value["status"] == "partial"
    assert value["findings"] == []
    assert value["sources"][0]["access_level"] == "original_text"
    assert value["sources"][0]["quote"] == "周一至周日 09:00–18:00 开放，需提前预约。"
    assert value["sources"][0]["retrieval_provenance"] == "kimi_search_pro"
    assert "儿童需由成人陪同入场" in value["sources"][0]["retrieved_text"]


@pytest.mark.asyncio
async def test_p3_search_pro_makes_no_automatic_retry_after_failure() -> None:
    posts = 0
    hook_calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(503, text="temporary failure")

    async def before(payload: dict[str, object]) -> None:
        nonlocal hook_calls
        hook_calls += 1

    async def after(body: object) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderAnalysisError) as raised:
            await WritingTransport(ConfiguredKeychain(), client).complete(
                stage="P3", system="P3", user="{}", provider_id="kimi",
                model_id="kimi-k2.6", max_output_tokens=100,
                before_request=before, after_response=after,
                research_task={"task_key": "r1", "question": "test"},
            )

    assert raised.value.code == "provider_unavailable"
    assert hook_calls == 1
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
async def test_search_pro_empty_results_fail_closed_without_invented_sources() -> None:

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"search_results": []})

    async def before(payload: dict[str, object]) -> None:
        return None

    async def after(body: object) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        raw = await WritingTransport(ConfiguredKeychain(), client).complete(
            stage="P3", system="P3", user="{}", provider_id="kimi",
            model_id="kimi-k2.6", max_output_tokens=100,
            before_request=before, after_response=after,
            research_task={"task_key": "r1", "question": "unknown"},
        )

    assert json.loads(raw) == {
        "task_key": "r1", "status": "not_found",
        "answer": "未取得可追溯的网页原文片段。",
        "findings": [], "sources": [],
        "unresolved_questions": ["搜索未返回可用的网页原文片段。"],
    }


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
