from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import delete, select, update

from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    ProfileFact,
    ReanalysisBatch,
    TempFileManifest,
    TodoTombstone,
)


class HistoryBusyError(RuntimeError):
    pass


class HistoryCleaner:
    def __init__(
        self,
        database: Database,
        audio_root: Path,
        staging_root: Path | None = None,
        *,
        task_coordinator=None,
    ) -> None:
        self.database = database
        self.audio_root = audio_root
        self.staging_root = staging_root
        self.task_coordinator = task_coordinator
        self._lock = asyncio.Lock()

    async def clear(self, *, confirm: bool) -> None:
        if not confirm:
            raise ValueError("Clear history requires confirmation")
        async with self._cleanup_guard() as profile_retry_raced:
            if profile_retry_raced:
                raise HistoryBusyError(
                    "A profile rebuild raced history cleanup; retry after it finishes"
                )
            async with self._lock:
                async with self.database.session() as session:
                    async with session.begin():
                        active_analysis = await session.scalar(
                            select(AnalysisVersion.id)
                            .where(AnalysisVersion.status.in_(("pending", "running")))
                            .limit(1)
                        )
                        if active_analysis is not None:
                            raise HistoryBusyError(
                                "Pending or running analysis work must finish before "
                                "clearing history"
                            )
                        active_reanalysis = await session.scalar(
                        select(ReanalysisBatch.id)
                        .where(
                            ReanalysisBatch.status.in_(
                                (
                                    "pending",
                                    "running",
                                    "paused",
                                    "stopping",
                                    "paused_credential_changed",
                                    "paused_rules_changed",
                                    "paused_error",
                                )
                            )
                        )
                        .limit(1)
                    )
                        if active_reanalysis is not None:
                            raise HistoryBusyError(
                                "History reanalysis must stop before clearing history"
                            )
                        await session.execute(
                            update(Batch).values(current_analysis_version_id=None)
                        )
                        await session.execute(delete(ReanalysisBatch))
                        await session.execute(delete(AnalysisVersion))
                        await session.execute(delete(Batch))
                        await session.execute(delete(ProfileFact))
                        await session.execute(delete(TodoTombstone))
                        await session.execute(delete(TempFileManifest))
                        await session.execute(delete(AnalysisJob))
                self._clear_root(self.audio_root)
                if self.staging_root is not None:
                    self._clear_root(self.staging_root)

    @asynccontextmanager
    async def _cleanup_guard(self):
        guard = getattr(self.task_coordinator, "history_cleanup_guard", None)
        if guard is None:
            yield False
            return
        async with guard() as profile_retry_raced:
            yield profile_retry_raced

    @staticmethod
    def _clear_root(path: Path) -> None:
        root = path.resolve(strict=False)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in root.iterdir():
            resolved = child.resolve(strict=False)
            if root not in resolved.parents:
                continue
            if resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
