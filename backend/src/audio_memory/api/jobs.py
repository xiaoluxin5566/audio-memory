from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import select

from audio_memory.analysis.errors import ANALYSIS_RETRYABLE_ERROR_CODES
from audio_memory.analysis.task_coordinator import AlreadyRunningError, AnalysisRequest
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisVersion
from audio_memory.prompts.composer import PromptComposer
from audio_memory.transcript_safety import safe_active_profile_facts
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
    live_progress_percent: float = 0.0
    eta_state: str = "unavailable"
    eta_seconds: int | None = None
    local_phase: str | None = None
    batch_current: int = 0
    batch_total: int = 0
    sleep_prevention_status: str | None = None


def service_from(request: Request) -> UploadService:
    return request.app.state.upload_service


async def has_legacy_completed_unaudited_report(
    request: Request, job_id: str
) -> bool:
    async with service_from(request).database.session() as session:
        version = await session.scalar(
            select(AnalysisVersion)
            .where(
                AnalysisVersion.source_job_id == job_id,
                AnalysisVersion.reanalysis_batch_id.is_(None),
                AnalysisVersion.status.in_(("completed", "failed")),
            )
            .order_by(AnalysisVersion.created_at.desc())
            .limit(1)
        )
    if version is None:
        return False
    try:
        staged = json.loads(version.staged_results_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(staged.get("direct_report_v1_markdown"), str):
        return False
    if version.status == "failed":
        return version.error_code in ANALYSIS_RETRYABLE_ERROR_CODES
    metadata = staged.get("direct_report_publication_metadata")
    return isinstance(metadata, dict) and (
        metadata.get("audit_status") == "completed_unaudited"
    )


async def protect_job_if_enabled(request: Request, job_id: str) -> str:
    enabled = await request.app.state.settings_repository.prevent_sleep_enabled()
    if not enabled:
        return "disabled"
    return await request.app.state.sleep_prevention.acquire(job_id)


async def job_view_with_sleep_status(request: Request, job) -> JobView:
    view = JobView.model_validate(job, from_attributes=True)
    if job.stage in {JobStage.TRANSCRIBING.value, JobStage.ANALYZING.value}:
        enabled = await request.app.state.settings_repository.prevent_sleep_enabled()
        view.sleep_prevention_status = (
            request.app.state.sleep_prevention.status if enabled else "disabled"
        )
    return view


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
                    "Analysis pipeline failed job_id=%s "
                    "diagnostic=pipeline_failed error_type=%s",
                    job_id,
                    type(error).__name__,
                )

    task.add_done_callback(finish)


async def run_pipeline(
    request: Request,
    job_id: str,
    analysis_request: AnalysisRequest,
    *,
    resume: bool = False,
    sleep_protected: bool = False,
) -> None:
    transcription = request.app.state.transcription_service
    engine = request.app.state.whisper_engine
    submitted = False
    try:
        if resume:
            await transcription.resume_job(job_id, engine)
        else:
            await transcription.run_job(job_id, engine)
        await request.app.state.analysis_task_coordinator.submit_new_upload(
            analysis_request
        )
        submitted = True
    finally:
        if sleep_protected and not submitted:
            await request.app.state.sleep_prevention.release(job_id)


async def snapshot_analysis_request(
    request: Request,
    *,
    job_id: str,
    provider_id: str,
    model_id: str,
    credential_generation: int,
) -> AnalysisRequest:
    prompts = {
        "user-analysis-goal": {
            "version": 1,
            "content": PromptComposer.default_user_analysis_goal(),
        },
        "direct-report": {
            "version": 1,
            "content": PromptComposer._fixed_prompt("direct-report.md"),
        },
        "direct-report-system": {
            "version": 1,
            "content": PromptComposer._fixed_prompt("direct-report-system.md"),
        },
    }
    database = request.app.state.database
    async with database.session() as session:
        facts = await safe_active_profile_facts(session)
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
    return await job_view_with_sleep_status(request, job) if job else None


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> JobView:
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await job_view_with_sleep_status(request, job)


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
        analysis_request = await snapshot_analysis_request(
            request,
            job_id=job_id,
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            credential_generation=credential_generation,
        )
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
    sleep_status = await protect_job_if_enabled(request, job_id)
    track_transcription(
        request,
        job_id,
        run_pipeline(
            request,
            job_id,
            analysis_request,
            sleep_protected=sleep_status == "active",
        ),
    )
    view = JobView.model_validate(job, from_attributes=True)
    view.sleep_prevention_status = sleep_status
    return view


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
    analysis_request = await snapshot_analysis_request(
        request,
        job_id=job_id,
        provider_id=job.provider_id,
        model_id=job.model_id,
        credential_generation=await request.app.state.provider_coordinator.credential_generation(
            job.provider_id
        ),
    )
    sleep_status = await protect_job_if_enabled(request, job_id)
    track_transcription(
        request,
        job_id,
        run_pipeline(
            request,
            job_id,
            analysis_request,
            resume=True,
            sleep_protected=sleep_status == "active",
        ),
    )
    return {
        "id": job_id,
        "stage": JobStage.TRANSCRIBING.value,
        "sleep_prevention_status": sleep_status,
    }


@router.post("/{job_id}/retry-analysis", status_code=202)
async def retry_analysis(job_id: str, request: Request) -> dict[str, str]:
    tasks: dict[str, asyncio.Task[None]] = request.app.state.transcription_tasks
    if job_id in tasks:
        raise HTTPException(status_code=409, detail="Analysis is already running")
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    legacy_completed_unaudited = (
        job.stage in {JobStage.COMPLETED.value, JobStage.INTERRUPTED.value}
        and await has_legacy_completed_unaudited_report(request, job_id)
    )
    if (
        job.stage != JobStage.FAILED.value
        or job.error_code not in ANALYSIS_RETRYABLE_ERROR_CODES
    ) and not legacy_completed_unaudited:
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

    resumed_version = (
        await request.app.state.analysis_task_coordinator.retry_failed_upload_in_place(
            source_job_id=job_id,
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            credential_generation=credential_generation,
        )
    )
    if resumed_version is not None:
        return {"id": job_id, "stage": JobStage.ANALYZING.value}

    analysis_request = await snapshot_analysis_request(
        request,
        job_id=job_id,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        credential_generation=credential_generation,
    )
    sleep_status = await protect_job_if_enabled(request, job_id)
    submitted = False
    try:
        if job.error_code.startswith("autonomous_"):
            resumed_version = (
                await request.app.state.analysis_task_coordinator.retry_failed_upload_in_place(
                    source_job_id=job_id,
                    provider_id=provider.provider_id,
                    model_id=provider.model_id,
                    credential_generation=credential_generation,
                )
            )
            if resumed_version is not None:
                submitted = True
                return {
                    "id": job_id,
                    "stage": JobStage.ANALYZING.value,
                    "sleep_prevention_status": sleep_status,
                }

        await request.app.state.analysis_task_coordinator.submit_new_upload(
            analysis_request
        )
        submitted = True
        return {
            "id": job_id,
            "stage": JobStage.ANALYZING.value,
            "sleep_prevention_status": sleep_status,
        }
    finally:
        if sleep_status == "active" and not submitted:
            await request.app.state.sleep_prevention.release(job_id)


@router.delete("/{job_id}", status_code=204)
async def cancel_job(job_id: str, request: Request) -> Response:
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if job.stage == JobStage.ANALYZING.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "analysis_publication_in_progress",
                "message": "报告生成已进入发布阶段，完成前不能取消",
            },
        )
    tasks: dict[str, asyncio.Task[None]] = request.app.state.transcription_tasks
    task = tasks.get(job_id)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        await request.app.state.analysis_task_coordinator.cancel_new_upload(job_id)
        await service_from(request).cancel_job(job_id)
    except (LookupError, AlreadyRunningError) as exc:
        status = 409 if isinstance(exc, AlreadyRunningError) else 404
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    finally:
        await request.app.state.sleep_prevention.release(job_id)
    return Response(status_code=204)
