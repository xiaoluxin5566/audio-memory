from __future__ import annotations

import pytest

from audio_memory.asr.credentials import (
    ASR_KEYCHAIN_ID,
    INSTALLATION_KEYCHAIN_ID,
    AsrCredentialCoordinator,
)
from audio_memory.asr.types import AsrStateValue, AsrValidationResult
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class MemoryKeychain:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def read(self, credential_id: str) -> KeychainReadResult:
        value = self.values.get(credential_id)
        if value is None:
            return KeychainReadResult(KeychainStatus.UNCONFIGURED)
        return KeychainReadResult(KeychainStatus.CONFIGURED, value)

    def replace(self, credential_id: str, candidate: bytes) -> None:
        self.values[credential_id] = candidate


class RecordingValidator:
    def __init__(self, accepted: bytes = b"valid-asr-key") -> None:
        self.accepted = accepted
        self.calls: list[bytes] = []

    async def validate(self, candidate: bytes) -> AsrValidationResult:
        self.calls.append(candidate)
        if candidate == self.accepted:
            return AsrValidationResult(ok=True)
        return AsrValidationResult(ok=False, error_code="invalid_api_key")


@pytest.mark.asyncio
async def test_valid_candidate_replaces_saved_asr_key_only() -> None:
    keychain = MemoryKeychain()
    keychain.values[ASR_KEYCHAIN_ID] = b"old-asr-key"
    keychain.values[INSTALLATION_KEYCHAIN_ID] = b"installation-token"
    coordinator = AsrCredentialCoordinator(
        keychain=keychain, validator=RecordingValidator()
    )

    result = await coordinator.validate_candidate(b"valid-asr-key")

    assert result.ok is True
    assert keychain.values[ASR_KEYCHAIN_ID] == b"valid-asr-key"
    assert keychain.values[INSTALLATION_KEYCHAIN_ID] == b"installation-token"
    assert coordinator.state().state is AsrStateValue.AVAILABLE


@pytest.mark.asyncio
async def test_invalid_candidate_does_not_replace_saved_key() -> None:
    keychain = MemoryKeychain()
    keychain.values[ASR_KEYCHAIN_ID] = b"old-asr-key"
    coordinator = AsrCredentialCoordinator(
        keychain=keychain, validator=RecordingValidator()
    )

    result = await coordinator.validate_candidate(b"wrong")

    assert result.ok is False
    assert keychain.values[ASR_KEYCHAIN_ID] == b"old-asr-key"
    assert coordinator.state().state is AsrStateValue.UNAVAILABLE
    assert coordinator.state().error_code == "invalid_api_key"


def test_state_never_contains_saved_secret() -> None:
    keychain = MemoryKeychain()
    keychain.values[ASR_KEYCHAIN_ID] = b"never-return-this"
    coordinator = AsrCredentialCoordinator(
        keychain=keychain, validator=RecordingValidator()
    )

    state = coordinator.state()

    assert state.state is AsrStateValue.UNCONFIGURED
    assert "never-return-this" not in repr(state)

