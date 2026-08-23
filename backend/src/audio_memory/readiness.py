from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from audio_memory.asr.types import AsrState, AsrStateValue
from audio_memory.providers.types import ProviderState, ProviderStateName


class AnalysisReadinessSource(Protocol):
    async def snapshot_active_with_generation(self) -> tuple[ProviderState, int]: ...


class AsrReadinessSource(Protocol):
    def state(self) -> AsrState: ...


@dataclass(frozen=True, slots=True)
class PipelineReadinessView:
    ready: bool
    analysis_ready: bool
    asr_ready: bool
    missing: tuple[str, ...]


@dataclass(slots=True)
class PipelineReadiness:
    analysis: AnalysisReadinessSource
    asr: AsrReadinessSource

    async def check(self) -> PipelineReadinessView:
        try:
            provider, _generation = (
                await self.analysis.snapshot_active_with_generation()
            )
            analysis_ready = provider.state is ProviderStateName.AVAILABLE
        except LookupError:
            analysis_ready = False
        asr_ready = self.asr.state().state is AsrStateValue.AVAILABLE
        missing: list[str] = []
        if not analysis_ready:
            missing.append("analysis")
        if not asr_ready:
            missing.append("asr:volcano")
        return PipelineReadinessView(
            ready=not missing,
            analysis_ready=analysis_ready,
            asr_ready=asr_ready,
            missing=tuple(missing),
        )

