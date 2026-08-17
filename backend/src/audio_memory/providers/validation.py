from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from audio_memory.providers.types import (
    ProviderConfig,
    ProviderId,
    ValidationErrorCode,
    ValidationResult,
)
from audio_memory.providers.adapters.base import ChatCompletionsAdapter


USER_MESSAGES = {
    ValidationErrorCode.INVALID_KEY: "API Key 无效，请重新填写",
    ValidationErrorCode.IP_NOT_AUTHORIZED: "当前网络 IP 未获此 OpenAI 组织授权",
    ValidationErrorCode.PERMISSION_DENIED: "当前账户无模型访问权限",
    ValidationErrorCode.INSUFFICIENT_BALANCE: "当前账户余额不足",
    ValidationErrorCode.RATE_LIMITED: "请求过于频繁，请稍后重试",
    ValidationErrorCode.NETWORK_ERROR: "网络连接失败，请检查网络后重试",
    ValidationErrorCode.PROVIDER_UNAVAILABLE: "模型服务暂时不可用，请稍后重试",
    ValidationErrorCode.TIMEOUT: "校验超时，请重新校验",
    ValidationErrorCode.VALIDATION_PROTOCOL_ERROR: "模型校验响应异常，请重新校验",
    ValidationErrorCode.KEYCHAIN_UNAVAILABLE: "无法访问系统钥匙串，请解锁 Mac 或检查系统权限",
    ValidationErrorCode.UNKNOWN: "校验失败，请重新尝试",
}


@dataclass(slots=True)
class ProviderValidationService:
    config: ProviderConfig
    client: httpx.AsyncClient
    adapter: ChatCompletionsAdapter | None = None

    @classmethod
    def for_test(
        cls, *, provider_id: str, endpoint: str, model_id: str
    ) -> ProviderValidationService:
        return cls(
            ProviderConfig(ProviderId(provider_id), provider_id, endpoint, model_id),
            httpx.AsyncClient(timeout=15.0),
        )

    async def validate(self, secret: bytes) -> ValidationResult:
        return await self.validate_model(secret, model_id=self.config.model_id)

    async def validate_model(
        self, secret: bytes, *, model_id: str | None = None
    ) -> ValidationResult:
        selected_model = model_id or self.config.model_id
        try:
            response = await self.client.post(
                self.config.endpoint,
                headers={
                    "Authorization": f"Bearer {secret.decode('utf-8')}",
                    "Content-Type": "application/json",
                },
                json=(
                    self.adapter or ChatCompletionsAdapter(self.config)
                ).validation_payload(model_id=selected_model),
            )
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return self._error(ValidationErrorCode.TIMEOUT)
        except httpx.RequestError:
            return self._error(ValidationErrorCode.NETWORK_ERROR)

        provider_error_code = self._safe_provider_error_code(response)
        if (
            response.status_code == 401
            and self.config.provider_id is ProviderId.OPENAI
            and provider_error_code == "ip_not_authorized"
        ):
            return self._error(ValidationErrorCode.IP_NOT_AUTHORIZED)
        if (
            response.status_code == 401
            and self.config.provider_id is ProviderId.OPENAI
            and provider_error_code is not None
        ):
            return ValidationResult(
                ok=False,
                error_code=ValidationErrorCode.OPENAI_AUTHENTICATION_REJECTED,
                message=f"OpenAI 拒绝了认证（原因代码：{provider_error_code}）",
            )
        if response.status_code == 401 and provider_error_code is not None:
            return ValidationResult(
                ok=False,
                error_code=ValidationErrorCode.PROVIDER_AUTHENTICATION_REJECTED,
                message=(
                    f"{self.config.display_name} 拒绝了认证"
                    f"（原因代码：{provider_error_code}）"
                ),
            )
        if response.status_code == 401:
            return self._error(ValidationErrorCode.INVALID_KEY)
        if response.status_code == 403:
            return self._error(ValidationErrorCode.PERMISSION_DENIED)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
            return self._error(ValidationErrorCode.RATE_LIMITED, seconds)
        if response.status_code >= 500:
            return self._error(ValidationErrorCode.PROVIDER_UNAVAILABLE)
        if response.is_error and provider_error_code is not None:
            return ValidationResult(
                ok=False,
                error_code=ValidationErrorCode.PROVIDER_REQUEST_REJECTED,
                message=(
                    f"{self.config.display_name} 拒绝了校验请求"
                    f"（HTTP {response.status_code}，原因代码：{provider_error_code}）"
                ),
            )
        if response.is_error:
            return self._error(ValidationErrorCode.UNKNOWN)

        try:
            body = response.json()
            content = (self.adapter or ChatCompletionsAdapter(self.config)).extract_text(body)
        except (ValueError, KeyError, IndexError, TypeError):
            return self._error(ValidationErrorCode.VALIDATION_PROTOCOL_ERROR)
        if not isinstance(content, str) or content.strip().casefold() != "ok":
            return self._error(ValidationErrorCode.VALIDATION_PROTOCOL_ERROR)
        return ValidationResult(ok=True)

    @staticmethod
    def _safe_provider_error_code(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except (TypeError, ValueError):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
            return None
        error = body["error"]
        candidate = error.get("code") or error.get("type")
        if not isinstance(candidate, str):
            return None
        normalized = candidate.strip().casefold()
        if not re.fullmatch(r"[a-z0-9_]{1,80}", normalized):
            return None
        return normalized

    @staticmethod
    def _error(
        code: ValidationErrorCode, retry_after: int | None = None
    ) -> ValidationResult:
        return ValidationResult(
            ok=False,
            error_code=code,
            message=USER_MESSAGES[code],
            retry_after_seconds=retry_after,
        )
