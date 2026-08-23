from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from audio_memory.asr.credentials import AsrCredentialCoordinator
from audio_memory.asr.types import AsrState


router = APIRouter(prefix="/api/asr", tags=["asr"])


class AsrView(BaseModel):
    provider_id: str
    display_name: str
    resource_id: str
    state: str
    last_validated_at: datetime | None
    error_code: str | None

    @classmethod
    def from_state(cls, state: AsrState) -> AsrView:
        return cls(
            provider_id=state.provider_id.value,
            display_name=state.display_name,
            resource_id=state.resource_id,
            state=state.state.value,
            last_validated_at=state.last_validated_at,
            error_code=state.error_code,
        )


class AsrKeyInput(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)


def coordinator_from(request: Request) -> AsrCredentialCoordinator:
    return request.app.state.asr_coordinator


@router.get("")
async def get_asr_state(request: Request) -> AsrView:
    return AsrView.from_state(coordinator_from(request).state())


@router.put("/key")
async def save_asr_key(payload: AsrKeyInput, request: Request) -> AsrView:
    coordinator = coordinator_from(request)
    result = await coordinator.validate_candidate(payload.api_key.encode("utf-8"))
    if not result.ok:
        raise HTTPException(
            status_code=422,
            detail={"code": result.error_code or "validation_failed"},
        )
    return AsrView.from_state(coordinator.state())


@router.post("/validate")
async def validate_saved_asr_key(request: Request) -> AsrView:
    coordinator = coordinator_from(request)
    result = await coordinator.validate_saved()
    if not result.ok:
        raise HTTPException(
            status_code=422,
            detail={"code": result.error_code or "validation_failed"},
        )
    return AsrView.from_state(coordinator.state())

