from __future__ import annotations

import json
import logging
import asyncio
from unittest.mock import AsyncMock

import pytest

from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.observability import analysis_log_context, emit_analysis_event


def test_structured_event_inherits_context_and_uses_allowlisted_fields(caplog) -> None:
    logger = logging.getLogger("audio-memory-observability-test")
    caplog.set_level(logging.INFO, logger=logger.name)

    with analysis_log_context(
        job_id="job-1",
        analysis_version_id="version-1",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
    ):
        emit_analysis_event(
            logger,
            "analysis.provider.request_started",
            status="started",
            elapsed_ms=12,
        )

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "analysis.provider.request_started"
    assert payload["job_id"] == "job-1"
    assert payload["analysis_version_id"] == "version-1"
    assert payload["provider_id"] == "deepseek"
    assert payload["model_id"] == "deepseek-v4-pro"
    assert payload["status"] == "started"
    assert payload["elapsed_ms"] == 12
    assert payload["timestamp"].endswith("+00:00")


def test_structured_event_never_serializes_content_or_exception_message(caplog) -> None:
    logger = logging.getLogger("audio-memory-observability-redaction-test")
    caplog.set_level(logging.INFO, logger=logger.name)
    secret_values = (
        "TRANSCRIPT-SECRET",
        "PROMPT-SECRET",
        "MODEL-OUTPUT-SECRET",
        "API-KEY-SECRET",
        "EXCEPTION-MESSAGE-SECRET",
    )

    emit_analysis_event(
        logger,
        "analysis.job.failed",
        status="failed",
        error=RuntimeError(secret_values[-1]),
        transcript=secret_values[0],
        prompt=secret_values[1],
        model_output=secret_values[2],
        api_key=secret_values[3],
    )

    rendered = caplog.records[-1].message
    payload = json.loads(rendered)
    assert payload["error_type"] == "RuntimeError"
    assert all(secret not in rendered for secret in secret_values)
    assert set(payload).isdisjoint(
        {"transcript", "prompt", "model_output", "api_key"}
    )


@pytest.mark.asyncio
async def test_provider_request_events_inherit_worker_context_without_content(
    caplog,
) -> None:
    client = object.__new__(ProviderAnalysisClient)
    client._remote_lock = asyncio.Lock()
    client._parallel_audit_limit = asyncio.Semaphore(1)
    client._generate_serialized = AsyncMock(return_value="MODEL-OUTPUT-SECRET")
    logger = logging.getLogger("uvicorn.error")
    caplog.set_level(logging.INFO, logger=logger.name)

    with analysis_log_context(
        job_id="job-provider",
        analysis_version_id="version-provider",
        queue_owner_id="owner-provider",
    ):
        result = await client.generate(
            "deepseek",
            model_id="deepseek-v4-pro",
            system="SYSTEM-PROMPT-SECRET",
            user="TRANSCRIPT-SECRET",
        )

    assert result == "MODEL-OUTPUT-SECRET"
    payloads = [json.loads(record.message) for record in caplog.records]
    assert [item["event"] for item in payloads] == [
        "analysis.provider.request_started",
        "analysis.provider.request_finished",
    ]
    assert all(item["job_id"] == "job-provider" for item in payloads)
    rendered = "\n".join(record.message for record in caplog.records)
    assert "SYSTEM-PROMPT-SECRET" not in rendered
    assert "TRANSCRIPT-SECRET" not in rendered
    assert "MODEL-OUTPUT-SECRET" not in rendered
