"""Single-request report reception with durable, explicitly incomplete checkpoints."""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from audio_memory.analysis.errors import ProviderAnalysisError


class ReportStream:
    def __init__(self):
        self.content, self.reasoning = [], []
        self.finish_reason = None
        self.usage = None
        self.done = False
        self.events = 0
        self.metadata = {}
        self.last_event_raw = None

    def accept(self, data):
        self.last_event_raw = data
        if data == "[DONE]":
            if self.finish_reason is None:
                raise ValueError("Missing finish reason")
            self.done = True
            return
        value = json.loads(data)
        if not isinstance(value, dict) or "error" in value:
            raise ValueError("Invalid stream event")
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("Invalid stream choices")
        self.events += 1
        for key in ("id", "model", "created", "system_fingerprint"):
            if key in value:
                self.metadata[key] = value[key]
        if value.get("usage") is not None:
            if not isinstance(value["usage"], dict):
                raise ValueError("Invalid stream usage")
            self.usage = value["usage"]
        if not choices:
            return
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            raise ValueError("Unexpected stream choice")
        delta = choice.get("delta", {})
        if not isinstance(delta, dict) or delta.get("tool_calls"):
            raise ValueError("Unexpected report delta")
        for name, target in (("content", self.content), ("reasoning_content", self.reasoning)):
            fragment = delta.get(name)
            if fragment is not None:
                if not isinstance(fragment, str) or (fragment and self.finish_reason is not None):
                    raise ValueError("Invalid or late report content")
                target.append(fragment)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or self.finish_reason is not None:
                raise ValueError("Invalid finish reason")
            self.finish_reason = reason

    def body(self):
        body = {**self.metadata, "choices": [{"index": 0, "finish_reason": self.finish_reason,
            "message": {"role": "assistant", "content": "".join(self.content),
                        "reasoning_content": "".join(self.reasoning)}}]}
        if self.usage is not None:
            body["usage"] = self.usage
        return body


async def receive_report(client, endpoint, headers, payload, *, timeout_seconds,
                         progress=None, check_status):
    state = ReportStream()
    started = time.monotonic()
    last_checkpoint = started
    last_checkpoint_bytes = 0
    received_bytes = 0
    raw_parts = []
    fallback_body = None
    diagnostics = {"phase": "connecting", "http_status": None, "done_received": False}

    async def checkpoint(force=False):
        nonlocal last_checkpoint, last_checkpoint_bytes
        now = time.monotonic()
        if not force and now-last_checkpoint < 1 and received_bytes-last_checkpoint_bytes < 65536:
            return
        last_checkpoint, last_checkpoint_bytes = now, received_bytes
        if progress:
            await progress({"body": fallback_body or state.body(),
                "last_event_raw": state.last_event_raw,
                "raw_partial": "".join(raw_parts) if raw_parts else None,
                "diagnostics": {**diagnostics, "elapsed_seconds": round(now-started, 3),
                    "received_bytes": received_bytes, "event_count": state.events,
                    "done_received": state.done}})

    try:
        # A total deadline also bounds an endless stream of keep-alive frames.
        async with asyncio.timeout(timeout_seconds):
            async with client.stream("POST", endpoint, headers=headers, json=payload,
                    timeout=httpx.Timeout(timeout_seconds, connect=min(15, timeout_seconds),
                                          write=min(60, timeout_seconds), pool=min(15, timeout_seconds))) as response:
                diagnostics.update(phase="response_headers", http_status=response.status_code,
                    headers_after_seconds=round(time.monotonic()-started, 3),
                    http_version=response.http_version)
                request_id = response.headers.get("x-request-id", "")
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request_id):
                    diagnostics["request_id"] = request_id
                await checkpoint(True)
                if response.is_error:
                    await response.aread()
                    check_status(response)
                diagnostics["phase"] = "receiving"
                if response.headers.get("content-type", "").split(";", 1)[0].strip() == "text/event-stream":
                    frame = []
                    async for line in response.aiter_lines():
                        received_bytes += len(line.encode("utf-8")) + 1
                        if received_bytes > 16 * 1024 * 1024:
                            raise ValueError("Report response capacity exceeded")
                        if line.startswith("data:"):
                            frame.append(line[5:].removeprefix(" "))
                        elif line == "" and frame:
                            state.accept("\n".join(frame))
                            frame = []
                            await checkpoint(state.events == 1 or state.done)
                            if state.done:
                                break
                        else:
                            await checkpoint()
                    if not state.done:
                        raise ProviderAnalysisError("Provider stream ended before completion",
                                                    code="model_stream_incomplete")
                    body = state.body()
                else:
                    # Some compatible endpoints return a regular JSON envelope even
                    # when streaming was requested. Retain its bytes if it disconnects.
                    async for text in response.aiter_text():
                        received_bytes += len(text.encode("utf-8"))
                        if received_bytes > 16 * 1024 * 1024:
                            raise ValueError("Report response capacity exceeded")
                        raw_parts.append(text)
                        await checkpoint(len(raw_parts) == 1)
                    body = json.loads("".join(raw_parts))
                    fallback_body = body
                diagnostics["phase"] = "complete"
                await checkpoint(True)
                return body, response.status_code
    except (httpx.RequestError, TimeoutError, ValueError, ProviderAnalysisError, asyncio.CancelledError) as cause:
        if isinstance(cause, asyncio.CancelledError):
            error = cause
        elif isinstance(cause, ProviderAnalysisError):
            error = cause
        else:
            code = ("network_timeout" if isinstance(cause, (TimeoutError, httpx.TimeoutException))
                    else "provider_unavailable" if isinstance(cause, httpx.RequestError)
                    else "model_response_invalid")
            error = ProviderAnalysisError("Provider report reception failed", code=code)
        error.transport_error_type = "TotalDeadlineExceeded" if isinstance(cause, TimeoutError) else type(cause).__name__
        error.transport_diagnostics = {**diagnostics, "elapsed_seconds": round(time.monotonic()-started, 3),
            "received_bytes": received_bytes, "event_count": state.events, "done_received": state.done}
        error.http_status_code = diagnostics["http_status"]
        # A received HTTP error is a definite error response. Interrupted/invalid
        # 2xx streams remain unresolved and must never unlock another dispatch.
        error.response_incomplete = not (diagnostics["http_status"] and diagnostics["http_status"] >= 400)
        if getattr(error, "partial_response", None) is None:
            error.partial_response = "".join(raw_parts) if raw_parts else "".join(state.content)
        await checkpoint(True)
        if error is cause:
            raise
        raise error from cause
