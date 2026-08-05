from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse


@dataclass(frozen=True, slots=True)
class JobEvent:
    id: int
    event: str
    data: dict[str, object]


class JobEventBroker:
    def __init__(self) -> None:
        self._events: dict[str, list[JobEvent]] = defaultdict(list)
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    async def emit(self, job_id: str, event: str, data: dict[str, object]) -> JobEvent:
        item = JobEvent(len(self._events[job_id]) + 1, event, data)
        self._events[job_id].append(item)
        async with self._conditions[job_id]:
            self._conditions[job_id].notify_all()
        return item

    async def stream(self, job_id: str, after: int = 0):
        cursor = after
        while True:
            pending = [item for item in self._events[job_id] if item.id > cursor]
            if pending:
                for item in pending:
                    cursor = item.id
                    yield (
                        f"id: {item.id}\nevent: {item.event}\ndata: "
                        f"{json.dumps(item.data, ensure_ascii=False)}\n\n"
                    )
                continue
            try:
                async with self._conditions[job_id]:
                    await asyncio.wait_for(
                        self._conditions[job_id].wait(), timeout=15
                    )
            except TimeoutError:
                yield ": keepalive\n\n"


router = APIRouter(prefix="/api/jobs", tags=["job-events"])


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    last = request.headers.get("Last-Event-ID", "0")
    after = int(last) if last.isdigit() else 0
    broker: JobEventBroker = request.app.state.job_events
    return StreamingResponse(
        broker.stream(job_id, after), media_type="text/event-stream"
    )

