from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field as PydanticField

from audio_memory.uploads.service import UploadError, UploadService


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class FileView(BaseModel):
    id: str
    job_id: str
    original_name: str
    extension: str
    size_bytes: int
    duration_ms: int | None
    position: int
    upload_progress: int


class JobView(BaseModel):
    id: str
    stage: str
    error_code: str | None
    provider_id: str | None = None
    model_id: str | None = None
    files: list[FileView] = PydanticField(default_factory=list)


def service_from(request: Request) -> UploadService:
    return request.app.state.upload_service


@router.post("", status_code=201)
async def create_job(request: Request) -> JobView:
    job = await service_from(request).create_job()
    return JobView(id=job.id, stage=job.stage, error_code=job.error_code)


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> JobView:
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JobView.model_validate(job, from_attributes=True)


@router.post("/{job_id}/files", status_code=201, response_model=FileView)
async def upload_file(
    job_id: str, request: Request, file: UploadFile = File(...)
) -> FileView | JSONResponse:
    try:
        uploaded = await service_from(request).upload(job_id, file)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UploadError as exc:
        status = 415 if exc.code == "unsupported_format" else 409
        return JSONResponse(
            status_code=status,
            content={
                "detail": {
                    "code": exc.code,
                    "message": str(exc),
                    "file_id": exc.file_id,
                }
            },
        )
    return FileView.model_validate(uploaded, from_attributes=True)


@router.delete("/{job_id}/files/{file_id}", status_code=204)
async def delete_file(job_id: str, file_id: str, request: Request) -> Response:
    try:
        await service_from(request).remove_file(job_id, file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/{job_id}/start")
async def start_job(job_id: str, request: Request) -> JobView:
    coordinator = request.app.state.provider_coordinator
    try:
        provider = await coordinator.snapshot_active()
        validation = await coordinator.validate_saved(provider.provider_id)
        if not validation.ok:
            raise UploadError("当前模型不可用，请修改配置或重新校验", code="provider_unavailable")
        job = await service_from(request).start(
            job_id,
            provider_id=provider.provider_id,
            model_id=provider.model_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UploadError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return JobView.model_validate(job, from_attributes=True)
