from __future__ import annotations

import json

import pytest
from fastapi import FastAPI

from audio_memory.security.upload_readiness import ReadinessUploadMiddleware
from audio_memory.readiness import PipelineReadinessView


class FixedReadiness:
    async def check(self) -> PipelineReadinessView:
        return PipelineReadinessView(
            ready=False,
            analysis_ready=True,
            asr_ready=False,
            missing=("asr:volcano",),
        )


@pytest.mark.asyncio
async def test_upload_is_rejected_before_request_body_is_read() -> None:
    app = FastAPI()
    app.state.pipeline_readiness = FixedReadiness()
    app.add_middleware(ReadinessUploadMiddleware)
    receive_calls = 0
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"secret audio", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/jobs/job-1/files",
            "raw_path": b"/api/jobs/job-1/files",
            "query_string": b"",
            "headers": [(b"content-type", b"application/octet-stream")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8766),
            "app": app,
        },
        receive,
        send,
    )

    assert receive_calls == 0
    start = next(item for item in sent if item["type"] == "http.response.start")
    assert start["status"] == 409
    body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    assert json.loads(body) == {
        "detail": {
            "code": "configuration_required",
            "missing": ["asr:volcano"],
        }
    }

