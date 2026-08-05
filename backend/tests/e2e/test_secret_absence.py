from pathlib import Path

import pytest

from audio_memory.db import Database
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus
from audio_memory.providers.types import ValidationResult
from audio_memory.repositories import ProviderMetadataRepository


class MemoryKeychain:
    def __init__(self) -> None:
        self.values = {"kimi": None}

    def read(self, provider_id: str) -> KeychainReadResult:
        value = self.values[provider_id]
        if value is None:
            return KeychainReadResult(KeychainStatus.UNCONFIGURED)
        return KeychainReadResult(KeychainStatus.CONFIGURED, value)

    def replace(self, provider_id: str, candidate: bytes) -> None:
        self.values[provider_id] = candidate


class ValidProvider:
    async def validate(self, secret: bytes) -> ValidationResult:
        return ValidationResult(ok=True)


@pytest.mark.asyncio
async def test_api_key_never_reaches_sqlite_or_local_files(tmp_path: Path) -> None:
    secret = b"phase-one-secret-must-not-leak"
    database = Database(tmp_path / "audio-memory.sqlite3")
    await database.create_schema()
    metadata = ProviderMetadataRepository(database)
    await metadata.ensure_defaults({"kimi": "kimi-k2.5"})
    keychain = MemoryKeychain()
    coordinator = ProviderStateCoordinator(
        keychain=keychain,
        validators={"kimi": ValidProvider()},
        metadata=metadata,
    )

    result = await coordinator.validate_candidate("kimi", "window-a", secret)

    assert result.ok is True
    assert keychain.values["kimi"] == secret
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes(), f"secret leaked into {path}"
    await database.dispose()
