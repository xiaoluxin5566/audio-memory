from __future__ import annotations

import pytest

from audio_memory.db import Database
from audio_memory.repositories import ProviderMetadataRepository


@pytest.mark.asyncio
async def test_provider_activation_is_atomic_and_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "providers.sqlite3")
    await database.create_schema()
    repository = ProviderMetadataRepository(database)
    await repository.ensure_defaults(
        {"kimi": "kimi-k2.5", "deepseek": "deepseek-v4-flash", "openai": "gpt-5-mini"}
    )

    await repository.activate("kimi")
    await repository.activate("openai")
    await repository.activate("openai")
    rows = await repository.list_all()

    assert [row.provider_id for row in rows if row.active] == ["openai"]
    await database.dispose()


@pytest.mark.asyncio
async def test_validation_fields_are_updated_together(tmp_path) -> None:
    database = Database(tmp_path / "providers.sqlite3")
    await database.create_schema()
    repository = ProviderMetadataRepository(database)
    await repository.ensure_defaults({"kimi": "kimi-k2.5"})

    await repository.update_validation(
        "kimi",
        status="unavailable",
        validated_at="2026-08-05T12:00:00+00:00",
        error_code="invalid_key",
        error_message="API Key 无效，请重新填写",
    )
    row = next(item for item in await repository.list_all() if item.provider_id == "kimi")

    assert row.validation_status == "unavailable"
    assert row.last_validated_at == "2026-08-05T12:00:00+00:00"
    assert row.last_validation_error_code == "invalid_key"
    await database.dispose()

