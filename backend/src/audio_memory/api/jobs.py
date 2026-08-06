from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import select

from audio_memory.analysis.task_coordinator import AnalysisRequest
from audio_memory.domain import JobStage
from audio_memory.models import ProfileFact
from audio_memory.prompts.store import PROMPT_SCENES
from audio_memory.uploads.service import UploadError, UploadService


router = APIRouter(prefix="/api/jobs", tags=["jobs"])
logger = logging.getLogger("uvicorn.error")


class FileView(BaseModel):
    id: str
    job_id: str
    original_name: str
    extension: str
    size_bytes: int
    duration_ms: int | None
    recording_started_at: str | None
    recording_time_source: str
    timezone: str | None
    position: int
    upload_progress: int


class JobView(BaseModel):
    id: str
    stage: str
    error_code: str | None
    provider_id: str | None = None
    model_id: str | None = None
    files: list[FileView] = PydanticField(default_factory=list)
    progress_percent: int = 0
    eta_state: str = "unavailable"
    eta_seconds: int | None = None


def service_from(request: Request) -> UploadService:
    return request.app.state.upload_service


def track_transcription(request: Request, job_id: str, coroutine) -> None:
    tasks: dict[str, asyncio.Task[None]] = request.app.state.transcription_tasks
    task = asyncio.create_task(coroutine)
    tasks[job_id] = task

    def finish(completed: asyncio.Task[None]) -> None:
        tasks.pop(job_id, None)
        if not completed.cancelled():
            error = completed.exception()
            if error is not None:
                logger.error(
                    "Analysis pipeline failed for job %s: %s",
                    job_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

    task.add_done_callback(finish)


async def run_pipeline(
    request: Request,
    job_id: str,
    analysis_request: AnalysisRequest,
    *,
    resume: bool = False,
) -> None:
    transcription = request.app.state.transcription_service
    engine = request.app.state.whisper_engine
    if resume:
        await transcription.resume_job(job_id, engine)
    else:
        await transcription.run_job(job_id, engine)
    await request.app.state.analysis_task_coordinator.submit_new_upload(
        analysis_request
    )


async def snapshot_analysis_request(
    request: Request,
    *,
    job_id: str,
    provider_id: str,
    model_id: str,
    credential_generation: int,
) -> AnalysisRequest:
    prompt_store = request.app.state.prompt_store
    prompts = {
        scene_id: {
            "version": document.version,
            "content": document.content,
        }
        for scene_id in PROMPT_SCENES
        for document in [prompt_store.get(scene_id)]
    }
    database = request.app.state.database
    async with database.session() as session:
        facts = list(
            await session.scalars(
                select(ProfileFact).where(ProfileFact.status == "active")
            )
        )
    profile = [
        {
            "subject_id": fact.subject_id,
            "dimension": fact.dimension,
            "value": json.loads(fact.value_json),
            "confidence": fact.confidence,
            "origin": fact.origin,
        }
        for fact in facts
    ]
    return AnalysisRequest(
        source_job_id=job_id,
        source_batch_id=None,
        provider_id=provider_id,
        model_id=model_id,
        credential_generation=credential_generation,
        prompt_snapshot=prompts,
        profile_snapshot=profile,
        priority=0,
    )


@router.post("", status_code=201)
async def create_job(request: Request) -> JobView:
    job = await service_from(request).create_job()
    return JobView(id=job.id, stage=job.stage, error_code=job.error_code)


@router.get("/active", response_model=JobView | None)
async def get_active_job(request: Request) -> JobView | None:
    job = await service_from(request).get_active_job()
    return JobView.model_validate(job, from_attributes=True) if job else None


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> JobView:
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JobView.model_validate(job, from_attributes=True)


@router.post("/{job_id}/files", status_code=201, response_model=FileView)
async def upload_file(
    job_id: str,
    request: Request,
    file: UploadFile = File(...),
    file_modified: int | None = Form(None),
    timezone: str | None = Form(None),
) -> FileView | JSONResponse:
    try:
        uploaded = await service_from(request).upload(
            job_id,
            file,
            file_modified=file_modified,
            timezone=timezone,
        )
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
        provider, credential_generation = (
            await coordinator.snapshot_active_with_generation()
        )
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
    track_transcription(
        request,
        job_id,
        run_pipeline(
            request,
            job_id,
            await snapshot_analysis_request(
                request,
                job_id=job_id,
                provider_id=provider.provider_id,
                model_id=provider.model_id,
                credential_generation=credential_generation,
            ),
        ),
    )
    return JobView.model_validate(job, from_attributes=True)


@router.post("/{job_id}/resume", status_code=202)
async def resume_job(job_id: str, request: Request) -> dict[str, str]:
    tasks: dict[str, asyncio.Task[None]] = request.app.state.transcription_tasks
    if job_id in tasks:
        raise HTTPException(status_code=409, detail="Transcription is already running")
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if job.stage != JobStage.INTERRUPTED.value:
        raise HTTPException(
            status_code=409, detail="Only an interrupted transcription can resume"
        )
    if job.provider_id is None or job.model_id is None:
        raise HTTPException(status_code=409, detail="Job has no provider snapshot")
    track_transcription(
        request,
        job_id,
        run_pipeline(
            request,
            job_id,
            await snapshot_analysis_request(
                request,
                job_id=job_id,
                provider_id=job.provider_id,
                model_id=job.model_id,
                credential_generation=await request.app.state.provider_coordinator.credential_generation(
                    job.provider_id
                ),
            ),
            resume=True,
        ),
    )
    return {"id": job_id, "stage": JobStage.TRANSCRIBING.value}


@router.post("/{job_id}/retry-analysis", status_code=202)
async def retry_analysis(job_id: str, request: Request) -> dict[str, str]:
    tasks: dict[str, asyncio.Task[None]] = request.app.state.transcription_tasks
    if job_id in tasks:
        raise HTTPException(status_code=409, detail="Analysis is already running")
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    retryable_errors = {
        "model_analysis_failed",
        "credential_changed",
        "fixed_rules_changed",
    }
    if job.stage != JobStage.FAILED.value or job.error_code not in retryable_errors:
        raise HTTPException(
            status_code=409,
            detail="Only a failed model analysis can be retried",
        )

    coordinator = request.app.state.provider_coordinator
    try:
        provider, credential_generation = (
            await coordinator.snapshot_active_with_generation()
        )
        validation = await coordinator.validate_saved(provider.provider_id)
        if not validation.ok:
            raise UploadError(
                "当前模型不可用，请修改配置或重新校验",
                code="provider_unavailable",
            )
    except (LookupError, UploadError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": getattr(exc, "code", "provider_unavailable"),
                "message": str(exc),
            },
        ) from exc

    analysis_request = await snapshot_analysis_request(
        request,
        job_id=job_id,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        credential_generation=credential_generation,
    )
    await request.app.state.analysis_task_coordinator.submit_new_upload(
        analysis_request
    )
    return {"id": job_id, "stage": JobStage.ANALYZING.value}


@router.delete("/{job_id}", status_code=204)
async def cancel_job(job_id: str, request: Request) -> Response:
    tasks: dict[str, asyncio.Task[None]] = request.app.state.transcription_tasks
    task = tasks.get(job_id)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        await service_from(request).cancel_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)
