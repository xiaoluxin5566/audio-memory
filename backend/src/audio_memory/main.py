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
from audio_memory.config import AppPaths, assert_supported_platform
from audio_memory.db import Database, run_migrations
from audio_memory.instance_lock import InstanceLock
from audio_memory.api.providers import router as providers_router
from audio_memory.api.jobs import router as jobs_router
from audio_memory.api.events import JobEventBroker, router as events_router
from audio_memory.api.prompts import router as prompts_router
from audio_memory.api.content import router as content_router
from audio_memory.providers.adapters import DeepSeekAdapter, KimiAdapter, OpenAIAdapter
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
from audio_memory.providers.types import PROVIDER_CONFIGS
from audio_memory.providers.validation import ProviderValidationService
from audio_memory.repositories import ProviderMetadataRepository
from audio_memory.uploads.cleanup import cleanup_abandoned_uploads
from audio_memory.uploads.service import UploadService
from audio_memory.transcription.checkpoints import TranscriptionService
from audio_memory.transcription.engine import MLXWhisperEngine
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.prompts.store import PromptStore
from audio_memory.analysis.runner import AnalysisRunner
from audio_memory.analysis.task_coordinator import AnalysisTaskCoordinator
from audio_memory.analysis.provider import (
    ProviderAnalysisClient,
    RemoteProfileExtractor,
    RemoteQuestionAnswerer,
    RemoteSceneAnalyzer,
)
from audio_memory.analysis.publisher import AnalysisPublisher
from audio_memory.content.service import ContentService
from audio_memory.content.feedback import FeedbackWriter
from audio_memory.content.clear import HistoryCleaner


def create_app(
    *,
    paths: AppPaths | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    resolved_paths = paths or AppPaths.from_home(Path.home())
    resolved_frontend = frontend_dir or (
        Path(__file__).resolve().parents[3] / "prototype" / "dist" / "client"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        assert_supported_platform()
        resolved_paths.ensure_directories()
        instance_lock = InstanceLock(resolved_paths.lock)
        instance_lock.acquire()
        app.state.paths = resolved_paths
        app.state.instance_lock = instance_lock
        database: Database | None = None
        provider_clients: list[httpx.AsyncClient] = []
        whisper_engine: MLXWhisperEngine | None = None
        try:
            await asyncio.to_thread(run_migrations, resolved_paths.database)
            database = Database(resolved_paths.database)
            app.state.database = database
            await cleanup_abandoned_uploads(database, resolved_paths.staging)
            job_events = JobEventBroker()
            app.state.job_events = job_events
            eta_tracker = TranscriptionEtaTracker()
            app.state.eta_tracker = eta_tracker
            app.state.upload_service = UploadService(
                database, resolved_paths, job_events, eta_tracker=eta_tracker
            )
            whisper_engine = MLXWhisperEngine(
                database, resolved_paths, eta_tracker=eta_tracker
            )
            app.state.whisper_engine = whisper_engine
            transcription_service = TranscriptionService(
                database, eta_tracker=eta_tracker
            )
            await transcription_service.mark_abandoned_work_interrupted()
            app.state.transcription_service = transcription_service
            app.state.transcription_tasks = {}
            prompt_store = PromptStore(resolved_paths.prompts)
            await asyncio.to_thread(prompt_store.initialize)
            app.state.prompt_store = prompt_store
            adapters = {
                "kimi": KimiAdapter(PROVIDER_CONFIGS["kimi"]),
                "deepseek": DeepSeekAdapter(PROVIDER_CONFIGS["deepseek"]),
                "openai": OpenAIAdapter(PROVIDER_CONFIGS["openai"]),
            }
            validators = {}
            keychain_repository = KeychainRepository(MacSecurityClient())
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
            analysis_runner = AnalysisRunner(
                database=database,
                provider=RemoteSceneAnalyzer(analysis_client),
                profile_extractor=RemoteProfileExtractor(analysis_client),
                publisher=AnalysisPublisher(database, resolved_paths),
                generation_source=coordinator,
            )
            await coordinator.initialize()
            analysis_tasks = AnalysisTaskCoordinator(database)
            await analysis_tasks.start(analysis_runner)
            app.state.analysis_runner = analysis_runner
            app.state.analysis_task_coordinator = analysis_tasks
            app.state.content_service = ContentService(
                database,
                resolved_paths,
                RemoteQuestionAnswerer(analysis_client, coordinator),
            )
            app.state.feedback_writer = FeedbackWriter(
                database, resolved_paths.feedback
            )
            app.state.history_cleaner = HistoryCleaner(
                database, resolved_paths.audio, resolved_paths.staging
            )
            yield
        finally:
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
            if database is not None:
                await database.dispose()
            instance_lock.release()

    app = FastAPI(title="Audio Memory", version=__version__, lifespan=lifespan)
    app.include_router(providers_router)
    app.include_router(jobs_router)
    app.include_router(events_router)
    app.include_router(prompts_router)
    app.include_router(content_router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "platform": "macOS" if platform.system() == "Darwin" else platform.system(),
            "architecture": platform.machine(),
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
