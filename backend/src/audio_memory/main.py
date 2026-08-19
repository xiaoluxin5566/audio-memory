from __future__ import annotations

import asyncio
import platform
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
import httpx
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from audio_memory import __version__
from audio_memory.config import (
    AppPaths,
    AppProfile,
    PinnedDevelopmentRoot,
    RuntimeConfig,
    assert_supported_platform,
)
from audio_memory.db import Database, run_migrations
from audio_memory.instance_lock import InstanceLock
from audio_memory.api.providers import router as providers_router
from audio_memory.api.jobs import router as jobs_router
from audio_memory.api.events import JobEventBroker, router as events_router
from audio_memory.api.prompts import router as prompts_router
from audio_memory.api.content import router as content_router
from audio_memory.api.reanalysis import router as reanalysis_router
from audio_memory.api.settings import router as settings_router
from audio_memory.providers.adapters import DeepSeekAdapter, GLMAdapter, KimiAdapter, OpenAIAdapter
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
from audio_memory.providers.types import PROVIDER_CONFIGS
from audio_memory.providers.validation import ProviderValidationService
from audio_memory.repositories import AppSettingsRepository, ProviderMetadataRepository
from audio_memory.power.sleep_prevention import SleepPreventionManager
from audio_memory.uploads.cleanup import cleanup_abandoned_uploads
from audio_memory.uploads.service import UploadService
from audio_memory.transcription.checkpoints import TranscriptionService
from audio_memory.transcription.engine import MLXWhisperEngine
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.transcription.risk_service import TranscriptionRiskGateService
from audio_memory.prompts.store import PromptStore
from audio_memory.analysis.single_report_runner import SingleReportRunner
from audio_memory.analysis.task_coordinator import AnalysisTaskCoordinator
from audio_memory.analysis.provider import (
    ProviderAnalysisClient,
    RemoteQuestionAnswerer,
)
from audio_memory.analysis.publisher import AnalysisPublisher
from audio_memory.content.service import ContentService
from audio_memory.content.feedback import FeedbackWriter
from audio_memory.content.clear import HistoryCleaner
from audio_memory.security.local_session import LocalSessionSecurity
from audio_memory.security.middleware import LocalWebSecurityMiddleware
from audio_memory.reanalysis.preview import ReanalysisPreviewBuilder
from audio_memory.reanalysis.service import ReanalysisService
from audio_memory.reanalysis.worker import ReanalysisWorker


def create_app(
    *,
    runtime_config: RuntimeConfig | None = None,
    paths: AppPaths | None = None,
    frontend_dir: Path | None = None,
    local_port: int | None = None,
) -> FastAPI:
    base_runtime_config = runtime_config or RuntimeConfig.from_environment(
        home=Path.home(),
        project_root=Path(__file__).resolve().parents[3],
    )
    resolved_runtime_config = base_runtime_config.with_overrides(
        paths=paths,
        port=local_port,
    )
    resolved_paths = resolved_runtime_config.paths
    resolved_port = resolved_runtime_config.port
    resolved_frontend = frontend_dir or (
        Path(__file__).resolve().parents[3] / "prototype" / "dist" / "client"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        assert_supported_platform()
        resolved_runtime_config.validate()
        development_boundary: PinnedDevelopmentRoot | None = None
        instance_lock: InstanceLock | None = None
        database: Database | None = None
        provider_clients: list[httpx.AsyncClient] = []
        whisper_engine: MLXWhisperEngine | None = None
        try:
            if resolved_runtime_config.profile is AppProfile.DEVELOPMENT:
                development_boundary = PinnedDevelopmentRoot.open(
                    resolved_runtime_config, create=True
                )
                assert development_boundary is not None
                development_boundary.ensure_directories()
            else:
                resolved_paths.ensure_directories()
            if development_boundary is not None:
                development_boundary.verify()
            instance_lock = InstanceLock(
                resolved_paths.lock,
                write_boundary=development_boundary,
            )
            instance_lock.acquire()
            app.state.paths = resolved_paths
            app.state.instance_lock = instance_lock
            local_security.write_boundary = development_boundary
            if development_boundary is not None:
                development_boundary.verify()
            await asyncio.to_thread(
                run_migrations,
                resolved_paths.database,
                write_boundary=development_boundary,
            )
            if development_boundary is not None:
                development_boundary.verify()
            database = Database(
                resolved_paths.database,
                write_boundary=development_boundary,
            )
            app.state.database = database
            app.state.settings_repository = AppSettingsRepository(database)
            sleep_prevention = SleepPreventionManager()
            app.state.sleep_prevention = sleep_prevention

            async def protect_upload(job_id: str) -> None:
                if await app.state.settings_repository.prevent_sleep_enabled():
                    await sleep_prevention.acquire(job_id)
            await cleanup_abandoned_uploads(
                database,
                resolved_paths.staging,
                write_boundary=development_boundary,
            )
            job_events = JobEventBroker()
            app.state.job_events = job_events
            eta_tracker = TranscriptionEtaTracker()
            app.state.eta_tracker = eta_tracker
            app.state.upload_service = UploadService(
                database,
                resolved_paths,
                job_events,
                eta_tracker=eta_tracker,
                write_boundary=development_boundary,
            )
            whisper_engine = MLXWhisperEngine(
                database,
                resolved_paths,
                runtime_profile=resolved_runtime_config.profile,
                eta_tracker=eta_tracker,
                write_boundary=development_boundary,
            )
            app.state.whisper_engine = whisper_engine
            transcription_service = TranscriptionService(
                database,
                eta_tracker=eta_tracker,
                risk_gate=TranscriptionRiskGateService(database),
            )
            await transcription_service.mark_abandoned_work_interrupted()
            app.state.transcription_service = transcription_service
            app.state.transcription_tasks = {}
            prompt_store = PromptStore(
                resolved_paths.prompts,
                write_boundary=development_boundary,
            )
            await asyncio.to_thread(prompt_store.initialize)
            app.state.prompt_store = prompt_store
            adapters = {
                "kimi": KimiAdapter(PROVIDER_CONFIGS["kimi"]),
                "deepseek": DeepSeekAdapter(PROVIDER_CONFIGS["deepseek"]),
                "openai": OpenAIAdapter(PROVIDER_CONFIGS["openai"]),
                "glm": GLMAdapter(PROVIDER_CONFIGS["glm"]),
            }
            validators = {}
            keychain_repository = KeychainRepository(
                MacSecurityClient(), service=resolved_runtime_config.keychain_service
            )
            for provider_id, config in PROVIDER_CONFIGS.items():
                client = httpx.AsyncClient(timeout=15.0)
                provider_clients.append(client)
                validators[provider_id] = ProviderValidationService(
                    config, client, adapters[provider_id]
                )
            coordinator = ProviderStateCoordinator(
                keychain=keychain_repository,
                validators=validators,
                metadata=ProviderMetadataRepository(database),
            )
            app.state.provider_coordinator = coordinator
            analysis_http_client = httpx.AsyncClient(timeout=120.0)
            provider_clients.append(analysis_http_client)
            analysis_client = ProviderAnalysisClient(
                keychain_repository, analysis_http_client
            )
            analysis_publisher = AnalysisPublisher(
                database,
                resolved_paths,
                write_boundary=development_boundary,
            )
            analysis_runner = SingleReportRunner(
                database=database,
                provider=analysis_client,
                publisher=analysis_publisher,
                generation_source=coordinator,
            )
            await coordinator.initialize()
            analysis_tasks = AnalysisTaskCoordinator(
                database,
                reclaim_foreign_on_initialize=True,
                on_upload_started=protect_upload,
                on_upload_finished=sleep_prevention.release,
            )
            reanalysis_worker = ReanalysisWorker(
                database=database,
                task_coordinator=analysis_tasks,
                publisher=analysis_publisher,
                provider_coordinator=coordinator,
            )
            await reanalysis_worker.start()
            await analysis_tasks.start(analysis_runner)
            app.state.analysis_runner = analysis_runner
            app.state.analysis_task_coordinator = analysis_tasks
            app.state.reanalysis_worker = reanalysis_worker
            app.state.reanalysis_service = ReanalysisService(
                database=database,
                preview_builder=ReanalysisPreviewBuilder(
                    database=database,
                    prompt_store=prompt_store,
                    provider_coordinator=coordinator,
                ),
                provider_coordinator=coordinator,
                task_coordinator=analysis_tasks,
                worker=reanalysis_worker,
                publisher=analysis_publisher,
            )
            app.state.content_service = ContentService(
                database,
                resolved_paths,
                RemoteQuestionAnswerer(analysis_client, coordinator),
                read_boundary=development_boundary,
            )
            app.state.feedback_writer = FeedbackWriter(
                database,
                resolved_paths.feedback,
                write_boundary=development_boundary,
            )
            app.state.history_cleaner = HistoryCleaner(
                database,
                resolved_paths.audio,
                resolved_paths.staging,
                task_coordinator=analysis_tasks,
                write_boundary=development_boundary,
            )
            yield
        finally:
            reanalysis_worker = getattr(app.state, "reanalysis_worker", None)
            if reanalysis_worker is not None:
                await reanalysis_worker.close()
            analysis_tasks = getattr(app.state, "analysis_task_coordinator", None)
            if analysis_tasks is not None:
                await analysis_tasks.close()
            for client in provider_clients:
                await client.aclose()
            for task in getattr(app.state, "transcription_tasks", {}).values():
                task.cancel()
            if getattr(app.state, "transcription_tasks", None):
                await asyncio.gather(
                    *app.state.transcription_tasks.values(), return_exceptions=True
                )
            if whisper_engine is not None:
                await whisper_engine.close()
            sleep_prevention = getattr(app.state, "sleep_prevention", None)
            if sleep_prevention is not None:
                await sleep_prevention.close()
            if database is not None:
                await database.dispose()
            if instance_lock is not None:
                instance_lock.release()
            if development_boundary is not None:
                development_boundary.close()

    app = FastAPI(title="Audio Memory", version=__version__, lifespan=lifespan)
    app.state.runtime_config = resolved_runtime_config
    local_security = LocalSessionSecurity(resolved_paths.local_session)
    app.state.local_web_security = local_security
    app.add_middleware(
        LocalWebSecurityMiddleware,
        security=local_security,
        allowed_port=resolved_port,
    )
    app.include_router(providers_router)
    app.include_router(jobs_router)
    app.include_router(events_router)
    app.include_router(prompts_router)
    app.include_router(content_router)
    app.include_router(reanalysis_router)
    app.include_router(settings_router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "platform": "macOS" if platform.system() == "Darwin" else platform.system(),
            "architecture": platform.machine(),
            "profile": resolved_runtime_config.profile.value,
        }

    if resolved_frontend.is_dir() and (resolved_frontend / "index.html").is_file():
        assets = resolved_frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/{frontend_path:path}", include_in_schema=False)
        async def frontend(frontend_path: str) -> FileResponse:
            if frontend_path not in {"", "history", "settings/prompts"}:
                raise HTTPException(status_code=404, detail="Not found")
            return FileResponse(resolved_frontend / "index.html")

    return app


app = create_app()
