from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from audio_memory.asr.types import AsrState, AsrStateValue
from audio_memory.providers.types import ProviderState, ProviderStateName


class AnalysisReadinessSource(Protocol):
    async def snapshot_active_with_generation(self) -> tuple[ProviderState, int]: ...


class AsrReadinessSource(Protocol):
    def state(self) -> AsrState: ...


class ManagedStorageReadinessSource(Protocol):
    @property
    def ready(self) -> bool: ...

    async def ensure_ready(self, *, force: bool = False) -> bool: ...


@dataclass(frozen=True, slots=True)
class PipelineReadinessView:
    ready: bool
    analysis_ready: bool
    asr_ready: bool
    missing: tuple[str, ...]
    managed_storage_ready: bool = True


@dataclass(slots=True)
class PipelineReadiness:
    analysis: AnalysisReadinessSource
    asr: AsrReadinessSource
    managed_storage: ManagedStorageReadinessSource | None = None

    async def check(
        self, *, refresh_managed_storage: bool = False
    ) -> PipelineReadinessView:
        if (
            refresh_managed_storage
            and self.managed_storage is not None
            and not self.managed_storage.ready
        ):
            await self.managed_storage.ensure_ready(force=True)
        try:
            provider, _generation = (
                await self.analysis.snapshot_active_with_generation()
            )
            analysis_ready = provider.state is ProviderStateName.AVAILABLE
        except LookupError:
            analysis_ready = False
        asr_ready = self.asr.state().state is AsrStateValue.AVAILABLE
        managed_storage_ready = (
            self.managed_storage is None or self.managed_storage.ready
        )
        missing: list[str] = []
        if not analysis_ready:
            missing.append("analysis")
        if not asr_ready:
            missing.append("asr:volcano")
        if not managed_storage_ready:
            missing.append("managed_storage")
        return PipelineReadinessView(
            ready=not missing,
            analysis_ready=analysis_ready,
            asr_ready=asr_ready,
            missing=tuple(missing),
            managed_storage_ready=managed_storage_ready,
        )
