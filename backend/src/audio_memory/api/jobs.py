from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import select

from audio_memory.analysis.errors import ANALYSIS_RETRYABLE_ERROR_CODES
from audio_memory.analysis.task_coordinator import AlreadyRunningError, AnalysisRequest
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, AnalysisVersion
from audio_memory.observability import emit_analysis_event
from audio_memory.prompts.composer import PromptComposer
from audio_memory.transcript_safety import safe_active_profile_facts
from audio_memory.uploads.service import UploadError, UploadService


router = APIRouter(prefix="/api/jobs", tags=["jobs"])
logger = logging.getLogger("uvicorn.error")
ANALYSIS_SUBMISSION_TIMEOUT_SECONDS = 30.0


def emit_duplicate_retry(job_id: str, retry_path: str) -> None:
    emit_analysis_event(
        logger,
        "analysis.retry.duplicate_accepted",
        job_id=job_id,
        status="already_running",
        retry_path=retry_path,
    )


def emit_accepted_retry(job_id: str, retry_path: str) -> None:
    emit_analysis_event(
        logger,
        "analysis.retry.accepted",
        job_id=job_id,
        status="analyzing",
        retry_path=retry_path,
    )


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
    analysis_phase: str | None = None
    analysis_detail_phase: str | None = None


def service_from(request: Request) -> UploadService:
    return request.app.state.upload_service


async def ensure_pipeline_ready(request: Request) -> None:
    readiness = getattr(request.app.state, "pipeline_readiness", None)
    if readiness is None:
        return
    result = await readiness.check()
    if not result.ready:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "configuration_required",
                "missing": list(result.missing),
            },
        )


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


async def has_active_analysis(request: Request, job_id: str) -> bool:
    async with service_from(request).database.session() as session:
        version_id = await session.scalar(
            select(AnalysisVersion.id).where(
                AnalysisVersion.source_job_id == job_id,
                AnalysisVersion.reanalysis_batch_id.is_(None),
                AnalysisVersion.status.in_(("pending", "running")),
            )
        )
    return version_id is not None


async def protect_job_if_enabled(request: Request, job_id: str) -> str:
    enabled = await request.app.state.settings_repository.prevent_sleep_enabled()
    if not enabled:
        return "disabled"
    return await request.app.state.sleep_prevention.acquire(job_id)


async def job_view_with_sleep_status(request: Request, job) -> JobView:
    view = JobView.model_validate(job, from_attributes=True)
    if job.stage == JobStage.READY_TO_COMMIT.value:
        view.analysis_phase = "running"
        view.analysis_detail_phase = "publishing"
    if job.stage in {JobStage.ANALYZING.value, JobStage.FAILED.value}:
        async with service_from(request).database.session() as session:
            version = await session.scalar(
                select(AnalysisVersion)
                .where(
                    AnalysisVersion.source_job_id == job.id,
                    AnalysisVersion.reanalysis_batch_id.is_(None),
                )
                .order_by(AnalysisVersion.created_at.desc())
                .limit(1)
            )
        if job.stage == JobStage.FAILED.value:
            view.analysis_phase = "failed"
        elif version is not None and version.status in {"pending", "running"}:
            view.analysis_phase = version.status
            if version.status == "running":
                try:
                    checkpoints = json.loads(version.pipeline_checkpoints_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    checkpoints = {}
                detail_phase = checkpoints.get("report_phase")
                if detail_phase in {"generating", "auditing", "revising", "publishing"}:
                    view.analysis_detail_phase = detail_phase
        else:
            view.analysis_phase = "failed"
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
    submitted = False
    pipeline_started_at = time.monotonic()
    try:
        if hasattr(request.app.state, "cloud_asr_coordinator"):
            cloud_asr = request.app.state.cloud_asr_coordinator
            if cloud_asr is None:
                async with request.app.state.database.session() as session:
                    job = await session.get(AnalysisJob, job_id)
                    if job is not None:
                        job.stage = JobStage.FAILED.value
                        job.error_code = "managed_storage_unavailable"
                        await session.commit()
                raise RuntimeError("managed OSS installation credential unavailable")
            await cloud_asr.run_job(
                job_id=job_id,
                analysis_request=analysis_request,
                analysis_submitter=request.app.state.analysis_task_coordinator,
            )
            submitted = True
            emit_analysis_event(
                logger,
                "transcription.completed",
                job_id=job_id,
                provider_id=getattr(analysis_request, "provider_id", None),
                model_id=getattr(analysis_request, "model_id", None),
                elapsed_ms=round((time.monotonic() - pipeline_started_at) * 1000),
                status="completed",
            )
            return

        transcription = request.app.state.transcription_service
        engine = request.app.state.whisper_engine
        if resume:
            await transcription.resume_job(job_id, engine)
        else:
            await transcription.run_job(job_id, engine)
        emit_analysis_event(
            logger,
            "transcription.completed",
            job_id=job_id,
            provider_id=getattr(analysis_request, "provider_id", None),
            model_id=getattr(analysis_request, "model_id", None),
            elapsed_ms=round((time.monotonic() - pipeline_started_at) * 1000),
            status="completed",
        )
        try:
            async with asyncio.timeout(ANALYSIS_SUBMISSION_TIMEOUT_SECONDS):
                await request.app.state.analysis_task_coordinator.submit_new_upload(
                    analysis_request
                )
        except BaseException as error:
            async with request.app.state.database.session() as session:
                durable_version_id = await session.scalar(
                    select(AnalysisVersion.id).where(
                        AnalysisVersion.source_job_id == job_id,
                        AnalysisVersion.reanalysis_batch_id.is_(None),
                        AnalysisVersion.status.in_(("pending", "running")),
                    )
                )
                if durable_version_id is not None:
                    submitted = True
                else:
                    job = await session.get(AnalysisJob, job_id)
                    if job is not None:
                        job.stage = JobStage.FAILED.value
                        job.error_code = "model_analysis_failed"
                        await session.commit()
            if not submitted:
                emit_analysis_event(
                    logger,
                    "analysis.job.failed",
                    job_id=job_id,
                    provider_id=getattr(analysis_request, "provider_id", None),
                    model_id=getattr(analysis_request, "model_id", None),
                    elapsed_ms=round(
                        (time.monotonic() - pipeline_started_at) * 1000
                    ),
                    status="failed",
                    error=error,
                )
            if submitted and not isinstance(error, asyncio.CancelledError):
                return
            raise
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


async def recover_cloud_asr_jobs(app) -> list[str]:
    cloud_asr = getattr(app.state, "cloud_asr_coordinator", None)
    if cloud_asr is None:
        return []
    request = Request({"type": "http", "app": app})
    recovered: list[str] = []
    for job_id in await cloud_asr.repository.recoverable_job_ids():
        try:
            job = await service_from(request).get_job(job_id)
            if job.provider_id is None or job.model_id is None:
                raise ValueError("cloud ASR job has no analysis provider snapshot")
            analysis_request = await snapshot_analysis_request(
                request,
                job_id=job_id,
                provider_id=job.provider_id,
                model_id=job.model_id,
                credential_generation=(
                    await app.state.provider_coordinator.credential_generation(
                        job.provider_id
                    )
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
                    sleep_protected=sleep_status == "active",
                ),
            )
            recovered.append(job_id)
        except Exception:
            logger.exception(
                "Cloud ASR startup recovery could not be scheduled job_id=%s",
                job_id,
            )
    return recovered


@router.post("", status_code=201)
async def create_job(request: Request) -> JobView:
    await ensure_pipeline_ready(request)
    try:
        job = await service_from(request).create_job()
    except UploadError as exc:
        detail = {"code": exc.code, "message": str(exc)}
        if exc.job_id is not None:
            detail["job_id"] = exc.job_id
        if exc.stage is not None:
            detail["stage"] = exc.stage
        raise HTTPException(status_code=409, detail=detail) from exc
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
    await ensure_pipeline_ready(request)
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
        status = {
            "unsupported_format": 415,
            "audio_runtime_unavailable": 503,
        }.get(exc.code, 409)
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
    except UploadError as exc:
        detail = {"code": exc.code, "message": str(exc)}
        if exc.stage is not None:
            detail["stage"] = exc.stage
        raise HTTPException(status_code=409, detail=detail) from exc
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
    try:
        await service_from(request).ensure_resume_sources_available(job_id)
    except UploadError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
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
async def retry_analysis(job_id: str, request: Request) -> dict[str, str | bool]:
    tasks: dict[str, asyncio.Task[None]] = request.app.state.transcription_tasks
    if job_id in tasks:
        raise HTTPException(status_code=409, detail="Analysis is already running")
    try:
        job = await service_from(request).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if job.stage == JobStage.ANALYZING.value and await has_active_analysis(
        request, job_id
    ):
        emit_duplicate_retry(job_id, "active_analysis_lookup")
        return {
            "id": job_id,
            "stage": JobStage.ANALYZING.value,
            "already_running": True,
        }
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

    try:
        resumed_version = (
            await request.app.state.analysis_task_coordinator.retry_failed_upload_in_place(
                source_job_id=job_id,
                provider_id=provider.provider_id,
                model_id=provider.model_id,
                credential_generation=credential_generation,
            )
        )
    except AlreadyRunningError:
        emit_duplicate_retry(job_id, "failed_upload_resume")
        return {
            "id": job_id,
            "stage": JobStage.ANALYZING.value,
            "already_running": True,
        }
    if resumed_version is not None:
        emit_accepted_retry(job_id, "failed_upload_resume")
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
                emit_accepted_retry(job_id, "autonomous_resume")
                return {
                    "id": job_id,
                    "stage": JobStage.ANALYZING.value,
                    "sleep_prevention_status": sleep_status,
                }

        try:
            await request.app.state.analysis_task_coordinator.submit_new_upload(
                analysis_request
            )
        except AlreadyRunningError:
            submitted = True
            emit_duplicate_retry(job_id, "new_upload_submission")
            return {
                "id": job_id,
                "stage": JobStage.ANALYZING.value,
                "sleep_prevention_status": sleep_status,
                "already_running": True,
            }
        submitted = True
        emit_accepted_retry(job_id, "new_upload_submission")
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
