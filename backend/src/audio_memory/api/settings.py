from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, StrictBool


router = APIRouter(prefix="/api/settings", tags=["settings"])


class AnalysisSettingsUpdate(BaseModel):
    prevent_sleep: StrictBool


class AnalysisSettingsView(BaseModel):
    prevent_sleep: bool
    sleep_prevention_status: str


def settings_view(request: Request, enabled: bool) -> AnalysisSettingsView:
    return AnalysisSettingsView(
        prevent_sleep=enabled,
        sleep_prevention_status=request.app.state.sleep_prevention.status,
    )


@router.get("/analysis")
async def get_analysis_settings(request: Request) -> AnalysisSettingsView:
    enabled = await request.app.state.settings_repository.prevent_sleep_enabled()
    return settings_view(request, enabled)


@router.put("/analysis")
async def update_analysis_settings(
    payload: AnalysisSettingsUpdate, request: Request
) -> AnalysisSettingsView:
    await request.app.state.settings_repository.set_prevent_sleep(
        payload.prevent_sleep
    )
    return settings_view(request, payload.prevent_sleep)
