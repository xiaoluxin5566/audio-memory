from __future__ import annotations

import asyncio
import platform
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from audio_memory import __version__
from audio_memory.config import AppPaths, assert_supported_platform
from audio_memory.db import Database, run_migrations
from audio_memory.instance_lock import InstanceLock


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
        try:
            await asyncio.to_thread(run_migrations, resolved_paths.database)
            database = Database(resolved_paths.database)
            app.state.database = database
            yield
        finally:
            if database is not None:
                await database.dispose()
            instance_lock.release()

    app = FastAPI(title="Audio Memory", version=__version__, lifespan=lifespan)

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
