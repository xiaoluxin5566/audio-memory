from __future__ import annotations

import asyncio
import platform
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
import httpx

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
from audio_memory.prompts.store import PromptStore
from audio_memory.analysis.orchestrator import AnalysisOrchestrator
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


def create_app(*, paths: AppPaths | None = None) -> FastAPI:
    resolved_paths = paths or AppPaths.from_home(Path.home())

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
        initialization_task: asyncio.Task[None] | None = None
        whisper_engine: MLXWhisperEngine | None = None
        try:
            await asyncio.to_thread(run_migrations, resolved_paths.database)
            database = Database(resolved_paths.database)
            app.state.database = database
            await cleanup_abandoned_uploads(database, resolved_paths.staging)
            job_events = JobEventBroker()
            app.state.job_events = job_events
            app.state.upload_service = UploadService(
                database, resolved_paths, job_events
            )
            whisper_engine = MLXWhisperEngine(database, resolved_paths)
            app.state.whisper_engine = whisper_engine
            transcription_service = TranscriptionService(database)
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
            app.state.analysis_orchestrator = AnalysisOrchestrator(
                database=database,
                prompt_store=prompt_store,
                analyzer=RemoteSceneAnalyzer(analysis_client),
                profile_extractor=RemoteProfileExtractor(analysis_client),
                publisher=AnalysisPublisher(database, resolved_paths),
            )
            app.state.content_service = ContentService(
                database,
                resolved_paths,
                RemoteQuestionAnswerer(analysis_client, coordinator),
            )
            app.state.feedback_writer = FeedbackWriter(
                database, resolved_paths.feedback
            )
            app.state.history_cleaner = HistoryCleaner(
                database, resolved_paths.audio
            )
            initialization_task = asyncio.create_task(coordinator.initialize())
            yield
        finally:
            if initialization_task is not None and not initialization_task.done():
                initialization_task.cancel()
                await asyncio.gather(initialization_task, return_exceptions=True)
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

    return app


app = create_app()
