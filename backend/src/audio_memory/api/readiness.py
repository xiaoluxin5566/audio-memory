from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel


router = APIRouter(prefix="/api/readiness", tags=["readiness"])


class ReadinessView(BaseModel):
    ready: bool
    analysis_ready: bool
    asr_ready: bool
    managed_storage_ready: bool
    missing: list[str]


@router.get("")
async def get_readiness(request: Request) -> ReadinessView:
    result = await request.app.state.pipeline_readiness.check()
    return ReadinessView(
        ready=result.ready,
        analysis_ready=result.analysis_ready,
        asr_ready=result.asr_ready,
        managed_storage_ready=result.managed_storage_ready,
        missing=list(result.missing),
    )
