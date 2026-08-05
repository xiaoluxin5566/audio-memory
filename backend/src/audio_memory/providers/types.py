from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProviderId(StrEnum):
    KIMI = "kimi"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"


class ProviderStateName(StrEnum):
    INITIALIZING = "initializing"
    UNCONFIGURED = "unconfigured"
    VALIDATING = "validating"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    KEYCHAIN_UNAVAILABLE = "keychain_unavailable"


class ValidationErrorCode(StrEnum):
    INVALID_KEY = "invalid_key"
    PERMISSION_DENIED = "permission_denied"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    VALIDATION_PROTOCOL_ERROR = "validation_protocol_error"
    KEYCHAIN_UNAVAILABLE = "keychain_unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: ProviderId
    display_name: str
    endpoint: str
    model_id: str
    api_style: str = "chat_completions"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    error_code: ValidationErrorCode | None = None
    message: str | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderState:
    provider_id: str
    display_name: str
    model_id: str
    active: bool = False
    state: ProviderStateName = ProviderStateName.INITIALIZING
    last_validated_at: datetime | None = None
    error_code: ValidationErrorCode | None = None
    error_message: str | None = None
    cooldown_until: datetime | None = None


PROVIDER_CONFIGS: dict[str, ProviderConfig] = {
    "kimi": ProviderConfig(
        ProviderId.KIMI,
        "Kimi",
        "https://api.moonshot.cn/v1/chat/completions",
        "kimi-k2.5",
    ),
    "deepseek": ProviderConfig(
        ProviderId.DEEPSEEK,
        "DeepSeek",
        "https://api.deepseek.com/chat/completions",
        "deepseek-v4-flash",
    ),
    "openai": ProviderConfig(
        ProviderId.OPENAI,
        "OpenAI",
        "https://api.openai.com/v1/responses",
        "gpt-5-mini",
        "responses",
    ),
}
