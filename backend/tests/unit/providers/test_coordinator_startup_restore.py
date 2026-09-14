from __future__ import annotations

import pytest

from audio_memory.db import Database
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.types import (
    ProviderStateName,
    ValidationErrorCode,
    ValidationResult,
)
from audio_memory.repositories import ProviderMetadataRepository


@pytest.mark.asyncio
async def test_initialize_without_validation_restores_metadata_without_credentials(
    tmp_path,
) -> None:
    database = Database(tmp_path / "provider-state-restore.sqlite3")
    await database.create_schema()
    metadata = ProviderMetadataRepository(database)
    await metadata.ensure_defaults(
        {
            "kimi": "kimi-k3",
            "deepseek": "deepseek-v4-pro",
            "openai": "gpt-5-mini",
            "glm": "glm-5.2",
        }
    )
    validated_at = "2026-09-09T06:30:00+00:00"
    await metadata.update_validation(
        "deepseek",
        status="unavailable",
        validated_at=validated_at,
        error_code="insufficient_balance",
        error_message="stored safe message",
    )
    await metadata.activate("deepseek")

    class ForbiddenKeychain:
        def read(self, provider_id: str):
            raise AssertionError("startup restore must not read Keychain")

    class ForbiddenValidator:
        async def validate(self, secret: bytes) -> ValidationResult:
            raise AssertionError("startup restore must not validate credentials")

    coordinator = ProviderStateCoordinator(
        keychain=ForbiddenKeychain(),
        validators={"deepseek": ForbiddenValidator()},
        metadata=metadata,
    )

    await coordinator.initialize(validate_credentials=False)

    state = coordinator.state("deepseek")
    assert state.state is ProviderStateName.UNAVAILABLE
    assert state.active is True
    assert state.last_validated_at.isoformat() == validated_at
    assert state.error_code is ValidationErrorCode.INSUFFICIENT_BALANCE
    assert state.error_message == "stored safe message"
    await database.dispose()
