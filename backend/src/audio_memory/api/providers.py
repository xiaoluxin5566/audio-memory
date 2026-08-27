from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.types import (
    CONFIGURABLE_PROVIDER_IDS,
    PROVIDER_CONFIGS,
    ProviderState,
)


router = APIRouter(prefix="/api/providers", tags=["providers"])


class ProviderView(BaseModel):
    provider_id: str
    display_name: str
    model_id: str
    active: bool
    state: str
    last_validated_at: datetime | None
    error_code: str | None
    error_message: str | None
    cooldown_until: datetime | None
    model_options: list[dict[str, str]]

    @classmethod
    def from_state(cls, state: ProviderState) -> ProviderView:
        return cls(
            provider_id=state.provider_id,
            display_name=state.display_name,
            model_id=state.model_id,
            active=state.active,
            state=state.state.value,
            last_validated_at=state.last_validated_at,
            error_code=state.error_code.value if state.error_code else None,
            error_message=state.error_message,
            cooldown_until=state.cooldown_until,
            model_options=[
                {"model_id": item.model_id, "label": item.label}
                for item in PROVIDER_CONFIGS[state.provider_id].models
            ],
        )


class ProviderList(BaseModel):
    providers: list[ProviderView]


class KeyInput(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)
    model_id: str | None = Field(default=None, min_length=1, max_length=120)


class ModelInput(BaseModel):
    model_id: str = Field(min_length=1, max_length=120)


def coordinator_from(request: Request) -> ProviderStateCoordinator:
    return request.app.state.provider_coordinator


def ensure_provider(provider_id: str) -> None:
    if provider_id not in CONFIGURABLE_PROVIDER_IDS:
        raise HTTPException(status_code=404, detail="Unsupported provider")


def configurable_states(coordinator: ProviderStateCoordinator) -> list[ProviderState]:
    return [coordinator.state(provider_id) for provider_id in CONFIGURABLE_PROVIDER_IDS]


@router.get("")
async def list_providers(request: Request) -> ProviderList:
    coordinator = coordinator_from(request)
    return ProviderList(
        providers=[ProviderView.from_state(item) for item in configurable_states(coordinator)]
    )


@router.post("/validate-configured")
async def validate_configured(request: Request) -> ProviderList:
    coordinator = coordinator_from(request)
    await asyncio.gather(
        *(
            asyncio.wait_for(coordinator.validate_saved(provider_id), timeout=20)
            for provider_id in CONFIGURABLE_PROVIDER_IDS
        )
    )
    return ProviderList(
        providers=[ProviderView.from_state(item) for item in configurable_states(coordinator)]
    )


@router.post("/{provider_id}/validate")
async def validate_provider(provider_id: str, request: Request) -> ProviderView:
    ensure_provider(provider_id)
    coordinator = coordinator_from(request)
    await asyncio.wait_for(coordinator.validate_saved(provider_id), timeout=20)
    return ProviderView.from_state(coordinator.state(provider_id))


@router.put("/{provider_id}/key")
async def save_provider_key(
    provider_id: str,
    payload: KeyInput,
    request: Request,
    session_id: Annotated[str, Header(alias="X-Configuration-Session")],
) -> ProviderView:
    ensure_provider(provider_id)
    coordinator = coordinator_from(request)
    try:
        result = await asyncio.wait_for(
            coordinator.validate_candidate(
                provider_id,
                session_id,
                payload.api_key.encode("utf-8"),
                model_id=payload.model_id,
            ),
            timeout=20,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not result.ok:
        raise HTTPException(
            status_code=422,
            detail={"code": result.error_code, "message": result.message},
        )
    return ProviderView.from_state(coordinator.state(provider_id))


@router.delete("/{provider_id}/candidate/{session_id}", status_code=204)
async def cancel_candidate(
    provider_id: str, session_id: str, request: Request
) -> None:
    ensure_provider(provider_id)
    await coordinator_from(request).cancel_candidate(provider_id, session_id)


@router.post("/{provider_id}/activate")
async def activate_provider(provider_id: str, request: Request) -> ProviderView:
    ensure_provider(provider_id)
    coordinator = coordinator_from(request)
    try:
        state = await coordinator.activate(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ProviderView.from_state(state)


@router.put("/{provider_id}/model")
async def select_provider_model(
    provider_id: str, payload: ModelInput, request: Request
) -> ProviderView:
    ensure_provider(provider_id)
    coordinator = coordinator_from(request)
    try:
        state = await asyncio.wait_for(
            coordinator.select_model(provider_id, payload.model_id), timeout=20
        )
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ProviderView.from_state(state)
