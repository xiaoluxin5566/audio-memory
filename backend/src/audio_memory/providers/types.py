from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProviderId(StrEnum):
    KIMI = "kimi"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    GLM = "glm"


class ProviderStateName(StrEnum):
    INITIALIZING = "initializing"
    UNCONFIGURED = "unconfigured"
    VALIDATING = "validating"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    KEYCHAIN_UNAVAILABLE = "keychain_unavailable"


class ValidationErrorCode(StrEnum):
    INVALID_KEY = "invalid_key"
    IP_NOT_AUTHORIZED = "ip_not_authorized"
    OPENAI_AUTHENTICATION_REJECTED = "openai_authentication_rejected"
    PROVIDER_AUTHENTICATION_REJECTED = "provider_authentication_rejected"
    PROVIDER_REQUEST_REJECTED = "provider_request_rejected"
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
class ProviderModel:
    model_id: str
    label: str


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: ProviderId
    display_name: str
    endpoint: str
    model_id: str
    api_style: str = "chat_completions"
    models: tuple[ProviderModel, ...] = ()

    def supports_model(self, model_id: str) -> bool:
        return any(item.model_id == model_id for item in self.models)


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
        "kimi-k3",
        models=(
            ProviderModel("kimi-k3", "最高质量"),
        ),
    ),
    "deepseek": ProviderConfig(
        ProviderId.DEEPSEEK,
        "DeepSeek",
        "https://api.deepseek.com/chat/completions",
        "deepseek-v4-pro",
        models=(
            ProviderModel("deepseek-v4-pro", "最高质量"),
        ),
    ),
    "openai": ProviderConfig(
        ProviderId.OPENAI,
        "OpenAI",
        "https://api.openai.com/v1/responses",
        "gpt-5-mini",
        "responses",
        (
            ProviderModel("gpt-5", "最高质量"),
            ProviderModel("gpt-5-mini", "最高性价比"),
        ),
    ),
    "glm": ProviderConfig(
        ProviderId.GLM,
        "GLM",
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "glm-5.2",
        models=(
            ProviderModel("glm-5.2", "最高质量"),
            ProviderModel("glm-4.7-flash", "最高性价比"),
        ),
    ),
}


# New configuration and analysis work is intentionally constrained to this
# release-safe catalog. The wider registry remains for frozen legacy work and
# historical provenance only.
CONFIGURABLE_PROVIDER_IDS: tuple[str, ...] = ("deepseek",)
