from __future__ import annotations

import pytest

from audio_memory.asr.types import (
    ASR_PROVIDER_CONFIGS,
    AsrProviderId,
    AsrState,
    AsrStateValue,
)
from audio_memory.providers.types import ProviderState, ProviderStateName
from audio_memory.readiness import PipelineReadiness


class AnalysisSource:
    def __init__(self, available: bool) -> None:
        self.available = available

    async def snapshot_active_with_generation(self):
        if not self.available:
            raise LookupError("missing")
        return (
            ProviderState(
                provider_id="deepseek",
                display_name="DeepSeek",
                model_id="deepseek-chat",
                active=True,
                state=ProviderStateName.AVAILABLE,
            ),
            1,
        )


class AsrSource:
    def __init__(self, available: bool) -> None:
        self.available = available

    def state(self) -> AsrState:
        config = ASR_PROVIDER_CONFIGS[AsrProviderId.VOLCANO]
        return AsrState(
            provider_id=config.provider_id,
            display_name=config.display_name,
            resource_id=config.resource_id,
            state=(
                AsrStateValue.AVAILABLE
                if self.available
                else AsrStateValue.UNCONFIGURED
            ),
        )


class StorageSource:
    def __init__(self, ready: bool) -> None:
        self.ready = ready


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("analysis", "asr", "ready", "missing"),
    [
        (True, True, True, ()),
        (False, True, False, ("analysis",)),
        (True, False, False, ("asr:volcano",)),
        (False, False, False, ("analysis", "asr:volcano")),
    ],
)
async def test_readiness_combinations(
    analysis: bool, asr: bool, ready: bool, missing: tuple[str, ...]
) -> None:
    result = await PipelineReadiness(
        analysis=AnalysisSource(analysis), asr=AsrSource(asr)
    ).check()

    assert result.ready is ready
    assert result.analysis_ready is analysis
    assert result.asr_ready is asr
    assert result.missing == missing


@pytest.mark.asyncio
async def test_managed_storage_is_part_of_upload_readiness() -> None:
    result = await PipelineReadiness(
        analysis=AnalysisSource(True),
        asr=AsrSource(True),
        managed_storage=StorageSource(False),
    ).check()

    assert result.ready is False
    assert result.managed_storage_ready is False
    assert result.missing == ("managed_storage",)
