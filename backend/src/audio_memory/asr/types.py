from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AsrProviderId(StrEnum):
    VOLCANO = "volcano"


@dataclass(frozen=True, slots=True)
class AsrProviderConfig:
    provider_id: AsrProviderId
    display_name: str
    resource_id: str
    max_duration_ms: int
    max_size_bytes: int
    supported_extensions: tuple[str, ...]


ASR_PROVIDER_CONFIGS = {
    AsrProviderId.VOLCANO: AsrProviderConfig(
        provider_id=AsrProviderId.VOLCANO,
        display_name="火山语音",
        resource_id="volc.seedasr.auc",
        max_duration_ms=5 * 60 * 60 * 1000,
        max_size_bytes=512 * 1024 * 1024,
        supported_extensions=(".mp3", ".aac"),
    )
}


class AsrStateValue(StrEnum):
    UNCONFIGURED = "unconfigured"
    VALIDATING = "validating"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    KEYCHAIN_UNAVAILABLE = "keychain_unavailable"


@dataclass(frozen=True, slots=True)
class AsrState:
    provider_id: AsrProviderId
    display_name: str
    resource_id: str
    state: AsrStateValue
    last_validated_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class AsrValidationResult:
    ok: bool
    error_code: str | None = None

