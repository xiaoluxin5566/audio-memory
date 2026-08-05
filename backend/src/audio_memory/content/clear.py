from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from sqlalchemy import delete

from audio_memory.db import Database
from audio_memory.models import AnalysisJob, Batch, ProfileFact


class HistoryCleaner:
    def __init__(self, database: Database, audio_root: Path) -> None:
        self.database = database
        self.audio_root = audio_root
        self._lock = asyncio.Lock()

    async def clear(self, *, confirm: bool) -> None:
        if not confirm:
            raise ValueError("Clear history requires confirmation")
        async with self._lock:
            async with self.database.session() as session:
                async with session.begin():
                    await session.execute(delete(Batch))
                    await session.execute(delete(ProfileFact))
                    await session.execute(delete(AnalysisJob))
            root = self.audio_root.resolve(strict=False)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for child in root.iterdir():
                resolved = child.resolve(strict=False)
                if root not in resolved.parents:
                    continue
                if resolved.is_dir():
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink()

