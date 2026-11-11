"""Unknown supplier usage remains unknown, never a zero-cost claim."""

from contextlib import contextmanager
from contextvars import ContextVar

_operation = ContextVar("recall_usage_operation", default=None)


def current_operation():
    return _operation.get()


@contextmanager
def operation_usage(kind, identity):
    token = _operation.set({"operation_kind": kind, "operation_id": identity})
    try:
        yield
    finally:
        _operation.reset(token)


def usage_record(body, *, purpose, provider, request_id=None, elapsed_ms=None):
    usage = body.get("usage") or {}
    return {
        "purpose": purpose,
        "provider": provider,
        "request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost": None,
        "cached": False,
    }


async def operation_attempts(database, kind, identity):
    import json

    from sqlalchemy import func, select

    from . import models as m

    async with database.session() as session:
        rows = (
            (
                await session.execute(
                    select(m.usage_attempts)
                    .where(
                        func.json_extract(
                            m.usage_attempts.c.usage_json, "$.operation_kind"
                        )
                        == kind,
                        func.json_extract(
                            m.usage_attempts.c.usage_json, "$.operation_id"
                        )
                        == identity,
                    )
                    .order_by(m.usage_attempts.c.created_at)
                )
            )
            .mappings()
            .all()
        )
    return [
        dict(
            json.loads(row["usage_json"]),
            purpose=row["purpose"],
            provider=row["provider"],
            request_id=row["request_id"],
            status=row["status"],
            error_code=row["error_code"],
        )
        for row in rows
    ]
