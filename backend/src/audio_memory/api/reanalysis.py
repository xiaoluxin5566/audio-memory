from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from audio_memory.reanalysis.service import (
    PreviewBlockedError,
    ReanalysisNotFoundError,
    ReanalysisStateError,
    SnapshotChangedError,
)
from audio_memory.reanalysis.preview import ReanalysisSourceSelectionError


router = APIRouter(prefix="/api/history/reanalysis-batches", tags=["reanalysis"])


class CreateReanalysisInput(BaseModel):
    preview_token: str = Field(min_length=1, max_length=16384)
    source_batch_ids: list[str] | None = Field(default=None, max_length=100)


def service_from(request: Request):
    return request.app.state.reanalysis_service


@router.get("/preview")
async def preview_reanalysis(
    request: Request,
    source_batch_ids: list[str] | None = Query(default=None),
) -> dict[str, object]:
    try:
        preview = await service_from(request).preview(
            None if source_batch_ids is None else tuple(source_batch_ids)
        )
    except ReanalysisSourceSelectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "provider_unavailable", "message": str(exc)},
        ) from exc
    return {
        "source_batch_ids": list(preview.source_batch_ids),
        "source_batch_count": preview.source_batch_count,
        "audio_file_count": preview.audio_file_count,
        "transcript_character_count": preview.transcript_character_count,
        "provider_id": preview.provider_id,
        "provider_display_name": preview.provider_display_name,
        "model_id": preview.model_id,
        "credential_generation": preview.credential_generation,
        "prompt_summary": {
            scene_id: asdict(summary)
            for scene_id, summary in preview.prompt_summary.items()
        },
        "estimated_calls_min": preview.estimated_calls_min,
        "estimated_calls_max": preview.estimated_calls_max,
        "whisper_calls": preview.whisper_calls,
        "diarization_calls": preview.diarization_calls,
        "blockers": preview.blockers,
        "preview_token": preview.preview_token,
        "snapshot_hash": preview.snapshot_hash,
        "expires_at": preview.expires_at,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_reanalysis(
    payload: CreateReanalysisInput, request: Request
) -> dict[str, object]:
    try:
        batch = await service_from(request).create_batch(
            payload.preview_token,
            None
            if payload.source_batch_ids is None
            else tuple(payload.source_batch_ids),
        )
    except ReanalysisSourceSelectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except SnapshotChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "snapshot_changed", "message": str(exc)},
        ) from exc
    except PreviewBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "reanalysis_blocked",
                "message": str(exc),
                "blockers": exc.blockers,
            },
        ) from exc
    return asdict(batch)


@router.get("/current", response_model=None)
async def current_reanalysis(request: Request) -> Response | dict[str, object]:
    batch = await service_from(request).current()
    if batch is None:
        return Response(status_code=204)
    return asdict(batch)


@router.post("/{batch_id}/stop")
async def stop_reanalysis(batch_id: str, request: Request) -> dict[str, object]:
    try:
        return asdict(await service_from(request).stop(batch_id))
    except (ReanalysisNotFoundError, ReanalysisStateError) as exc:
        raise _control_error(exc) from exc


@router.post("/{batch_id}/resume")
async def resume_reanalysis(batch_id: str, request: Request) -> dict[str, object]:
    try:
        return asdict(await service_from(request).resume(batch_id))
    except (ReanalysisNotFoundError, ReanalysisStateError) as exc:
        raise _control_error(exc) from exc


@router.post("/{batch_id}/retry-profile")
async def retry_reanalysis_profile(
    batch_id: str, request: Request
) -> dict[str, object]:
    try:
        return asdict(await service_from(request).retry_profile(batch_id))
    except (ReanalysisNotFoundError, ReanalysisStateError) as exc:
        raise _control_error(exc) from exc


def _control_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReanalysisNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": "reanalysis_not_found", "message": str(exc)},
        )
    if isinstance(exc, ReanalysisStateError):
        return HTTPException(
            status_code=409,
            detail={"code": "invalid_reanalysis_state", "message": str(exc)},
        )
    return HTTPException(status_code=500, detail="Unexpected reanalysis error")
