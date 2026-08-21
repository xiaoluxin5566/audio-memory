from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import json
import logging


_ALLOWED_FIELDS = frozenset(
    {
        "job_id",
        "analysis_version_id",
        "provider_id",
        "model_id",
        "elapsed_ms",
        "status",
        "error_type",
        "queue_owner_id",
        "lease_expires_at",
        "repair_type",
        "affected_count",
        "reason",
        "split_depth",
        "segment_count",
        "child_count",
        "chunk_index",
        "chunk_count",
        "audit_chunk_count",
        "retry_path",
    }
)
_CONTEXT: ContextVar[dict[str, str | int | float | bool]] = ContextVar(
    "audio_memory_analysis_log_context", default={}
)


@contextmanager
def analysis_log_context(**fields: object) -> Iterator[None]:
    safe = _safe_fields(fields)
    token = _CONTEXT.set({**_CONTEXT.get(), **safe})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def emit_analysis_event(
    logger: logging.Logger,
    event: str,
    *,
    error: BaseException | None = None,
    **fields: object,
) -> None:
    payload: dict[str, str | int | float | bool] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        **_CONTEXT.get(),
        **_safe_fields(fields),
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
    logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _safe_fields(
    fields: dict[str, object],
) -> dict[str, str | int | float | bool]:
    return {
        key: value
        for key, value in fields.items()
        if key in _ALLOWED_FIELDS
        and value is not None
        and isinstance(value, (str, int, float, bool))
    }
