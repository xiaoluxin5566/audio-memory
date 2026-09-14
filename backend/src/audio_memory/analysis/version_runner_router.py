from __future__ import annotations

from collections.abc import Mapping

from audio_memory.db import Database
from audio_memory.models import AnalysisVersion
from audio_memory.analysis.pipeline_identity import (
    BETA8_INDEXED_PIPELINE_KIND,
    BETA8_P1_P5_PIPELINE_KIND,
    BETA8_WRITING_PIPELINE_KIND,
    BETA8_LEGACY_PIPELINE_KIND,
    BETA8_PIPELINE_KINDS,
    KNOWN_PIPELINE_KINDS,
    SINGLE_REPORT_PIPELINE_KIND,
    validate_version_pipeline_identity,
)


class VersionRunnerRouter:
    def __init__(self, *, database: Database, runners: Mapping[str, object]) -> None:
        self.database = database
        self.runners = dict(runners)

    async def run(self, version_id: str, worker_owner_id: str):
        pipeline_kind = await self._pipeline_kind(version_id)
        if pipeline_kind not in KNOWN_PIPELINE_KINDS:
            raise ValueError(f"Unknown report pipeline: {pipeline_kind}")
        runner = self.runners.get(pipeline_kind)
        if runner is None:
            raise ValueError(f"Unknown report pipeline: {pipeline_kind}")
        return await runner.run(version_id, worker_owner_id)

    async def _pipeline_kind(self, version_id: str) -> str:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        if version is None:
            raise LookupError(f"Unknown analysis version: {version_id}")
        return validate_version_pipeline_identity(version).pipeline_kind
