from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from audio_memory.asr.types import (
    ASR_PROVIDER_CONFIGS,
    AsrProviderId,
    AsrState,
    AsrStateValue,
    AsrValidationResult,
)
from audio_memory.providers.keychain import (
    KeychainAccessError,
    KeychainReadResult,
    KeychainStatus,
)


ASR_KEYCHAIN_ID = "volcano_asr"
INSTALLATION_KEYCHAIN_ID = "beta_installation"


class CredentialStore(Protocol):
    def read(self, credential_id: str) -> KeychainReadResult: ...

    def replace(self, credential_id: str, candidate: bytes) -> None: ...


class AsrCredentialValidator(Protocol):
    async def validate(self, candidate: bytes) -> AsrValidationResult: ...


class AsrCredentialCoordinator:
    def __init__(
        self,
        *,
        keychain: CredentialStore,
        validator: AsrCredentialValidator,
    ) -> None:
        self._keychain = keychain
        self._validator = validator
        self._state = AsrStateValue.UNCONFIGURED
        self._last_validated_at: datetime | None = None
        self._error_code: str | None = None

    def state(self) -> AsrState:
        config = ASR_PROVIDER_CONFIGS[AsrProviderId.VOLCANO]
        return AsrState(
            provider_id=config.provider_id,
            display_name=config.display_name,
            resource_id=config.resource_id,
            state=self._state,
            last_validated_at=self._last_validated_at,
            error_code=self._error_code,
        )

    async def validate_candidate(self, candidate: bytes) -> AsrValidationResult:
        self._state = AsrStateValue.VALIDATING
        self._error_code = None
        result = await self._validator.validate(candidate)
        if not result.ok:
            self._state = AsrStateValue.UNAVAILABLE
            self._error_code = result.error_code or "validation_failed"
            return result
        try:
            self._keychain.replace(ASR_KEYCHAIN_ID, candidate)
        except KeychainAccessError:
            self._state = AsrStateValue.KEYCHAIN_UNAVAILABLE
            self._error_code = "keychain_unavailable"
            return AsrValidationResult(
                ok=False, error_code="keychain_unavailable"
            )
        self._state = AsrStateValue.AVAILABLE
        self._last_validated_at = datetime.now(UTC)
        return result

    async def validate_saved(self) -> AsrValidationResult:
        stored = self._keychain.read(ASR_KEYCHAIN_ID)
        if stored.status is KeychainStatus.UNCONFIGURED:
            self._state = AsrStateValue.UNCONFIGURED
            return AsrValidationResult(ok=False, error_code="unconfigured")
        if stored.status is not KeychainStatus.CONFIGURED or stored.secret is None:
            self._state = AsrStateValue.KEYCHAIN_UNAVAILABLE
            return AsrValidationResult(
                ok=False, error_code="keychain_unavailable"
            )
        self._state = AsrStateValue.VALIDATING
        result = await self._validator.validate(stored.secret)
        self._last_validated_at = datetime.now(UTC)
        if result.ok:
            self._state = AsrStateValue.AVAILABLE
            self._error_code = None
        else:
            self._state = AsrStateValue.UNAVAILABLE
            self._error_code = result.error_code or "validation_failed"
        return result

