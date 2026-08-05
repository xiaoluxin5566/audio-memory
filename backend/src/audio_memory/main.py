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
from audio_memory.providers.adapters import DeepSeekAdapter, KimiAdapter, OpenAIAdapter
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
from audio_memory.providers.types import PROVIDER_CONFIGS
from audio_memory.providers.validation import ProviderValidationService
from audio_memory.repositories import ProviderMetadataRepository


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
        try:
            await asyncio.to_thread(run_migrations, resolved_paths.database)
            database = Database(resolved_paths.database)
            app.state.database = database
            adapters = {
                "kimi": KimiAdapter(PROVIDER_CONFIGS["kimi"]),
                "deepseek": DeepSeekAdapter(PROVIDER_CONFIGS["deepseek"]),
                "openai": OpenAIAdapter(PROVIDER_CONFIGS["openai"]),
            }
            validators = {}
            for provider_id, config in PROVIDER_CONFIGS.items():
                client = httpx.AsyncClient(timeout=15.0)
                provider_clients.append(client)
                validators[provider_id] = ProviderValidationService(
                    config, client, adapters[provider_id]
                )
            coordinator = ProviderStateCoordinator(
                keychain=KeychainRepository(MacSecurityClient()),
                validators=validators,
                metadata=ProviderMetadataRepository(database),
            )
            app.state.provider_coordinator = coordinator
            initialization_task = asyncio.create_task(coordinator.initialize())
            yield
        finally:
            if initialization_task is not None and not initialization_task.done():
                initialization_task.cancel()
                await asyncio.gather(initialization_task, return_exceptions=True)
            for client in provider_clients:
                await client.aclose()
            if database is not None:
                await database.dispose()
            instance_lock.release()

    app = FastAPI(title="Audio Memory", version=__version__, lifespan=lifespan)
    app.include_router(providers_router)

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
