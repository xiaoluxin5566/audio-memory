from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.analysis.beta8_writing_stream import receive_report
from audio_memory.providers.adapters.deepseek import DeepSeekAdapter
from audio_memory.providers.keychain import KeychainStatus
from audio_memory.providers.types import PROVIDER_CONFIGS


RequestHook = Callable[[dict[str, object]], Awaitable[None]]
ResponseHook = Callable[[object], Awaitable[None]]
HostResolver = Callable[[str], Awaitable[Sequence[str]]]


def report_thinking(stage: str) -> dict[str, str]:
    """Reason deeply for discovery and use bounded reasoning for final writing."""
    return {"type": "enabled" if stage in {"P1", "P2", "P4"} else "disabled"}


def report_reasoning_effort(stage: str) -> str | None:
    if stage in {"P1", "P2", "P4"}:
        return "high"
    return None


class _Keychain(Protocol):
    def read(self, provider_id: str) -> object: ...


class WritingTransport:
    """One-attempt transport for the writing-only Beta 8 stages."""

    _REPORT_STAGES = frozenset({"P1", "P2", "P4", "P5"})
    _REPORT_TIMEOUT_SECONDS = 900
    _SEARCH_TIMEOUT_SECONDS = 120
    _SEARCH_PRO_TIMEOUT_SECONDS = 60

    def __init__(self, keychain: _Keychain, client: httpx.AsyncClient) -> None:
        self._keychain = keychain
        self._client = client
        self._adapters = {
            "deepseek": DeepSeekAdapter(PROVIDER_CONFIGS["deepseek"]),
        }

    async def complete(
        self,
        *,
        stage: str,
        system: str,
        user: str,
        provider_id: str,
        model_id: str,
        max_output_tokens: int,
        before_request: RequestHook,
        after_response: ResponseHook,
        on_progress: ResponseHook | None = None,
        research_task: Mapping[str, object] | None = None,
    ) -> str:
        self._validate_route(stage, provider_id, model_id, max_output_tokens)
        secret = self._credential(provider_id)
        if stage == "P3":
            return await self._complete_search_pro(
                research_task=research_task,
                secret=secret,
                before_request=before_request,
                after_response=after_response,
            )
        return await self._complete_report(
            stage=stage,
            system=system,
            user=user,
            model_id=model_id,
            max_output_tokens=max_output_tokens,
            secret=secret,
            before_request=before_request,
            after_response=after_response,
            on_progress=on_progress,
        )

    def _validate_route(
        self, stage: str, provider_id: str, model_id: str, max_output_tokens: int
    ) -> None:
        expected_provider = "kimi" if stage == "P3" else "deepseek"
        if stage not in self._REPORT_STAGES | {"P3"}:
            raise ProviderAnalysisError(
                "Writing stage is unsupported", code="writing_stage_unsupported"
            )
        if provider_id != expected_provider:
            raise ProviderAnalysisError(
                "Writing provider is unsupported for this stage",
                code="writing_provider_unsupported",
            )
        config = PROVIDER_CONFIGS[provider_id]
        if not config.supports_model(model_id):
            raise ProviderAnalysisError(
                "Writing model is unsupported", code="writing_model_unsupported"
            )
        if isinstance(max_output_tokens, bool) or max_output_tokens <= 0:
            raise ProviderAnalysisError(
                "Writing output budget is invalid", code="writing_budget_invalid"
            )

    def _credential(self, provider_id: str) -> bytes:
        try:
            read = self._keychain.read(provider_id)
        except Exception as exc:
            raise ProviderAnalysisError(
                "Provider credential is unavailable",
                code="keychain_unavailable",
                pause_batch=True,
            ) from exc
        secret = getattr(read, "secret", None)
        if (
            getattr(read, "status", None) is not KeychainStatus.CONFIGURED
            or not isinstance(secret, bytes)
            or not secret
        ):
            raise ProviderAnalysisError(
                "Provider credential is unavailable",
                code="keychain_unavailable",
                pause_batch=True,
            )
        return secret

    async def _complete_report(
        self,
        *,
        stage: str,
        system: str,
        user: str,
        model_id: str,
        max_output_tokens: int,
        secret: bytes,
        before_request: RequestHook,
        after_response: ResponseHook,
        on_progress: ResponseHook | None = None,
    ) -> str:
        adapter = self._adapters["deepseek"]
        payload = adapter.analysis_payload({
            "model": model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "thinking": report_thinking(stage),
        })
        effort = report_reasoning_effort(stage)
        if effort is not None:
            payload["reasoning_effort"] = effort
        try:
            authorization = secret.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderAnalysisError("Provider credential is unavailable",
                code="keychain_unavailable", pause_batch=True) from exc
        await before_request(payload)
        body, http_status = await receive_report(
            self._client, PROVIDER_CONFIGS["deepseek"].endpoint,
            {"Authorization": f"Bearer {authorization}", "Content-Type": "application/json"},
            payload, timeout_seconds=self._REPORT_TIMEOUT_SECONDS,
            progress=on_progress, check_status=self._raise_for_status,
        )
        await after_response(body)
        try:
            result = adapter.extract_result(body)
        except (TypeError, ValueError) as exc:
            error = ProviderAnalysisError(
                "Provider returned an invalid response", code="model_response_invalid"
            )
            error.http_status_code = http_status
            raise error from exc
        try:
            return self._accept_result(result.text, result.finish_reason)
        except ProviderAnalysisError as error:
            error.http_status_code = http_status
            raise

    async def _complete_search_pro(
        self,
        *,
        research_task: Mapping[str, object] | None,
        secret: bytes,
        before_request: RequestHook,
        after_response: ResponseHook,
    ) -> str:
        if not isinstance(research_task, Mapping):
            raise ProviderAnalysisError(
                "Research task is unavailable", code="model_response_invalid"
            )
        task_key = research_task.get("task_key")
        question = research_task.get("question")
        if not isinstance(task_key, str) or not task_key or not isinstance(question, str) or not question.strip():
            raise ProviderAnalysisError(
                "Research task is invalid", code="model_response_invalid"
            )
        payload = {
            "text_query": self._search_query(research_task),
            "limit": self._search_limit(research_task.get("source_limit")),
            "timeout_seconds": self._SEARCH_PRO_TIMEOUT_SECONDS,
        }
        endpoint = self._search_pro_endpoint()
        body, _http_status = await self._post(
            "kimi", payload, secret, before_request, after_response,
            timeout_seconds=self._SEARCH_TIMEOUT_SECONDS, endpoint=endpoint,
        )
        try:
            return json.dumps(
                self._search_pro_result(task_key, body),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ProviderAnalysisError(
                "Provider returned an invalid search response",
                code="model_response_invalid",
            ) from exc

    @staticmethod
    def _search_limit(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return min(20, max(1, value))
        return 5

    @staticmethod
    def _task_text(task: Mapping[str, object], key: str) -> str:
        value = task.get(key)
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _search_query(cls, task: Mapping[str, object]) -> str:
        context = cls._task_text(task, "public_context")
        question = cls._task_text(task, "question")
        prefix = f"{context} {question}" if context else question
        parts = [prefix]
        purpose = cls._task_text(task, "purpose")
        requirements = cls._task_text(task, "source_requirements")
        as_of = cls._task_text(task, "as_of")
        jurisdiction = cls._task_text(task, "jurisdiction")
        version = cls._task_text(task, "version_constraint")
        stop_condition = cls._task_text(task, "stop_condition")
        if purpose:
            parts.append(f"用于{purpose}。")
        if requirements:
            parts.append(f"条件：{requirements}。")
        if as_of:
            parts.append(f"截止日期：{as_of}。")
        if jurisdiction:
            parts.append(f"适用地区：{jurisdiction}。")
        if version:
            parts.append(f"版本要求：{version}。")
        if stop_condition:
            parts.append(f"需满足：{stop_condition}。")
        return " ".join(parts[:2]) + "".join(parts[2:])

    @staticmethod
    def _search_pro_endpoint() -> str:
        configured = urlsplit(PROVIDER_CONFIGS["kimi"].endpoint)
        return urlunsplit((configured.scheme, configured.netloc, "/v1/tools/search_pro", "", ""))

    @staticmethod
    def _search_pro_result(task_key: str, body: object) -> dict[str, object]:
        if not isinstance(body, Mapping) or not isinstance(body.get("search_results"), list):
            raise ValueError("search_results is missing")
        sources: list[dict[str, object]] = []
        for result in body["search_results"]:
            if not isinstance(result, Mapping):
                continue
            title, url = result.get("title"), result.get("url")
            chunks = result.get("chunks")
            if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.strip() or not isinstance(chunks, list):
                continue
            texts = [
                chunk["text"].strip() for chunk in chunks
                if isinstance(chunk, Mapping) and isinstance(chunk.get("text"), str) and chunk["text"].strip()
            ]
            if not texts:
                continue
            ordinal = len(sources) + 1
            sources.append({
                "local_source_key": f"search_pro_{ordinal}",
                "title": title.strip(),
                "url": url.strip(),
                "publisher": result.get("site_name") if isinstance(result.get("site_name"), str) else None,
                "published_at": result.get("date") if isinstance(result.get("date"), str) else None,
                "version": None,
                "access_level": "original_text",
                "quote": texts[0],
                "locator": None,
                "context_note": result.get("snippet") if isinstance(result.get("snippet"), str) else "Kimi Search Pro 返回的网页原文片段。",
                "retrieval_provenance": "kimi_search_pro",
                "retrieved_text": "\n\n".join(texts),
            })
        if not sources:
            return {
                "task_key": task_key,
                "status": "not_found",
                "answer": "未取得可追溯的网页原文片段。",
                "findings": [],
                "sources": [],
                "unresolved_questions": ["搜索未返回可用的网页原文片段。"],
            }
        return {
            "task_key": task_key,
            "status": "partial",
            "answer": f"取得 {len(sources)} 个可追溯来源的网页原文片段，由 P4 结合原始任务完成判断。",
            "findings": [],
            "sources": sources,
            "unresolved_questions": [],
        }

    async def _post(
        self,
        provider_id: str,
        payload: dict[str, object],
        secret: bytes,
        before_request: RequestHook,
        after_response: ResponseHook,
        *,
        timeout_seconds: float,
        endpoint: str | None = None,
    ) -> tuple[object, int]:
        try:
            authorization = secret.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderAnalysisError(
                "Provider credential is unavailable",
                code="keychain_unavailable",
                pause_batch=True,
            ) from exc
        await before_request(payload)
        try:
            response = await self._client.post(
                endpoint or PROVIDER_CONFIGS[provider_id].endpoint,
                headers={
                    "Authorization": f"Bearer {authorization}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            error = ProviderAnalysisError(
                "Provider network request timed out", code="network_timeout"
            )
            error.transport_error_type = type(exc).__name__
            raise error from exc
        except httpx.RequestError as exc:
            error = ProviderAnalysisError(
                "Provider network request failed", code="provider_unavailable"
            )
            error.transport_error_type = type(exc).__name__
            raise error from exc
        self._raise_for_status(response)
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            error = ProviderAnalysisError(
                "Provider returned an invalid response",
                code="model_response_invalid",
                partial_response=response.text,
            )
            error.http_status_code = response.status_code
            raise error from exc
        await after_response(body)
        return body, response.status_code

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if not response.is_error:
            return
        if response.status_code == 402:
            message, code, pause = (
                "Provider account balance is unavailable", "insufficient_balance", True
            )
        elif response.status_code in {401, 403}:
            message, code, pause = (
                "Provider credential or account is unavailable", "authentication_failed", True
            )
        elif response.status_code == 429:
            message, code, pause = (
                "Provider is temporarily unavailable", "rate_limited", True
            )
        elif response.status_code >= 500:
            message, code, pause = (
                "Provider is temporarily unavailable", "provider_unavailable", False
            )
        elif response.status_code in {400, 413, 422}:
            message, code, pause = (
                "Provider rejected the writing input", "provider_input_rejected", False
            )
        else:
            message, code, pause = (
                "Provider rejected the writing request", "content_rejected", False
            )
        error = ProviderAnalysisError(
            message,
            code=code,
            pause_batch=pause,
            partial_response=response.text,
        )
        error.http_status_code = response.status_code
        raise error

    @staticmethod
    def _accept_result(text: str, finish_reason: str | None) -> str:
        if finish_reason == "length":
            raise ProviderAnalysisError(
                "Provider output was truncated",
                code="model_output_truncated",
                partial_response=text,
            )
        if finish_reason in {"content_filter", "content_rejected"}:
            raise ProviderAnalysisError(
                "Provider rejected the writing content",
                code="content_rejected",
                partial_response=text or None,
            )
        if finish_reason != "stop":
            raise ProviderAnalysisError(
                "Provider returned an incomplete response",
                code="model_response_invalid",
                partial_response=text,
            )
        return text


class _TextExtractor(HTMLParser):
    _SKIPPED = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() in self._SKIPPED:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIPPED and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)


class SourceFetcher:
    """Fetch a bounded public text page without ambient proxy or credentials."""

    _REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
    # Clash uses the RFC 2544 benchmarking block as synthetic DNS answers.  It
    # is safe only when reached through a hostname: literal access remains
    # blocked below, and every redirect hostname is independently rechecked.
    _CLASH_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
    _TEXT_TYPES = (
        "text/",
        "application/json",
        "application/xml",
        "application/xhtml+xml",
    )

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
        max_bytes: int = 1_000_000,
        timeout_seconds: float = 10.0,
        max_redirects: int = 3,
    ) -> None:
        if max_bytes <= 0 or timeout_seconds <= 0 or max_redirects < 0:
            raise ValueError("Source fetch bounds must be positive")
        self._transport = transport
        self._resolver = resolver or self._resolve_host
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects

    async def fetch(self, url: str) -> dict[str, object]:
        fetched_at = datetime.now(timezone.utc).isoformat()
        async with httpx.AsyncClient(
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            http2=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            return await self._fetch_with_client(client, url, fetched_at)

    async def _fetch_with_client(
        self, client: httpx.AsyncClient, original_url: str, fetched_at: str
    ) -> dict[str, object]:
        current_url = original_url
        last_status: int | None = None
        resolved: dict[str, frozenset[str]] = {}
        for redirect_count in range(self._max_redirects + 1):
            safety_error = await self._check_url(current_url, resolved)
            if safety_error is not None:
                error = "unsafe_url" if redirect_count == 0 else "unsafe_redirect"
                return self._result(
                    status=last_status,
                    url=original_url,
                    final_url=current_url,
                    fetched_at=fetched_at,
                    error=error,
                    safety_reason=safety_error,
                )
            pinned_url, host, host_header = self._pinned_target(current_url, resolved)
            try:
                async with client.stream(
                    "GET",
                    pinned_url,
                    headers={
                        "Accept": "text/html,text/plain,application/xhtml+xml,application/json,application/xml",
                        "Connection": "close",
                        "Host": host_header,
                        "User-Agent": "AudioMemory-SourceVerifier/1.0",
                    },
                    follow_redirects=False,
                    timeout=self._timeout,
                    extensions={"sni_hostname": host},
                ) as response:
                    last_status = response.status_code
                    if response.status_code in self._REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            return self._result(
                                status=last_status, url=original_url,
                                final_url=current_url, fetched_at=fetched_at,
                                error="invalid_redirect",
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    if response.is_error:
                        return self._result(
                            status=last_status, url=original_url,
                            final_url=current_url, fetched_at=fetched_at,
                            error="http_error",
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not content_type.startswith(self._TEXT_TYPES):
                        return self._result(
                            status=last_status, url=original_url,
                            final_url=current_url, fetched_at=fetched_at,
                            error="unsupported_content_type",
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isascii() and declared.isdigit():
                        if int(declared) > self._max_bytes:
                            return self._result(
                                status=last_status, url=original_url,
                                final_url=current_url, fetched_at=fetched_at,
                                error="response_too_large",
                            )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_bytes:
                            return self._result(
                                status=last_status, url=original_url,
                                final_url=current_url, fetched_at=fetched_at,
                                error="response_too_large",
                            )
                        chunks.append(chunk)
                    charset = response.encoding or "utf-8"
                    raw_text = b"".join(chunks).decode(charset, errors="replace")
                    text = self._meaningful_text(raw_text, content_type)
                    return self._result(
                        status=last_status, url=original_url,
                        final_url=current_url, fetched_at=fetched_at,
                        text=text, error=None,
                    )
            except (httpx.TimeoutException, httpx.RequestError):
                return self._result(
                    status=last_status, url=original_url, final_url=current_url,
                    fetched_at=fetched_at, error="network_error",
                )
        return self._result(
            status=last_status,
            url=original_url,
            final_url=current_url,
            fetched_at=fetched_at,
            error="too_many_redirects",
        )

    async def _check_url(
        self, url: str, resolved: dict[str, frozenset[str]]
    ) -> str | None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError):
            return "invalid_url"
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "invalid_url"
        if parsed.username is not None or parsed.password is not None:
            return "credentialed_url"
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            return "nonstandard_port"
        try:
            host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            return "invalid_url"
        if (
            host == "localhost"
            or host.endswith((".localhost", ".local", ".internal", ".home", ".lan"))
        ):
            return "local_host"
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                addresses = frozenset(await self._resolver(host))
            except (OSError, ValueError, UnicodeError):
                return "dns_failure"
            if not addresses:
                return "dns_failure"
            try:
                if any(not self._safe_resolved_address(address) for address in addresses):
                    return "private_address"
            except ValueError:
                return "dns_failure"
            previous = resolved.get(host)
            if previous is not None and previous != addresses:
                return "dns_rebinding"
            resolved[host] = addresses
        else:
            if not literal.is_global:
                return "private_address"
        return None

    @classmethod
    def _safe_resolved_address(cls, address: str) -> bool:
        literal = ipaddress.ip_address(address)
        return literal.is_global or (
            isinstance(literal, ipaddress.IPv4Address)
            and literal in cls._CLASH_FAKE_IP_NETWORK
        )

    @staticmethod
    def _pinned_target(
        url: str, resolved: Mapping[str, frozenset[str]]
    ) -> tuple[str, str, str]:
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            address = sorted(resolved[host])[0]
        else:
            address = str(literal)
        pinned_host = f"[{address}]" if ":" in address else address
        netloc = pinned_host if parsed.port is None else f"{pinned_host}:{parsed.port}"
        host_header = host if parsed.port is None else f"{host}:{parsed.port}"
        return (
            urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, "")),
            host,
            host_header,
        )

    @staticmethod
    async def _resolve_host(host: str) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return tuple({record[4][0] for record in records})

    @staticmethod
    def _meaningful_text(raw_text: str, content_type: str) -> str:
        if "html" in content_type or "<html" in raw_text[:512].lower():
            parser = _TextExtractor()
            parser.feed(raw_text)
            parser.close()
            raw_text = " ".join(parser.parts)
        return " ".join(raw_text.split())

    @staticmethod
    def _result(
        *,
        status: int | None,
        url: str,
        final_url: str,
        fetched_at: str,
        error: str | None,
        text: str = "",
        safety_reason: str | None = None,
    ) -> dict[str, object]:
        return {
            "status": status,
            "url": url,
            "final_url": final_url,
            "text": text,
            "fetched_at": fetched_at,
            "content_sha256": sha256(text.encode("utf-8")).hexdigest() if text else None,
            "error": error,
            "safety_reason": safety_reason,
        }


def verify_source(
    candidate: Mapping[str, Any], fetched: Mapping[str, Any]
) -> dict[str, object]:
    """Keep model source claims separate from independently verified text."""

    quote = candidate.get("quote")
    fetched_text = fetched.get("text")
    normalized_quote = _normalize_text(quote) if isinstance(quote, str) else ""
    normalized_page = _normalize_text(fetched_text) if isinstance(fetched_text, str) else ""
    status = fetched.get("status")
    fetched_ok = (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 200 <= status < 300
        and fetched.get("error") is None
    )
    candidate_url = candidate.get("url")
    url_matches = (
        isinstance(candidate_url, str)
        and candidate_url == fetched.get("url")
    )
    verified = bool(
        candidate.get("access_level") == "original_text"
        and fetched_ok
        and url_matches
        and normalized_quote
        and normalized_quote in normalized_page
    )
    return {
        **dict(candidate),
        "quote_verified": verified,
        "verified_quote": normalized_quote if verified else None,
        "fetch": dict(fetched),
    }


def _normalize_text(value: str) -> str:
    return " ".join(value.split())
