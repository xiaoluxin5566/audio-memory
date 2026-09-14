from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from audio_memory.config import RuntimeConfig
from audio_memory.main import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [True, False])
async def test_development_startup_can_skip_connection_validation(
    preview,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool | None]] = []

    async def initialize_provider(_coordinator, *, validate_credentials=True) -> None:
        calls.append(("provider", validate_credentials))

    async def validate_asr(_coordinator) -> None:
        calls.append(("asr", None))

    async def skip_managed_storage(_runtime) -> None:
        calls.append(("managed_storage", None))

    monkeypatch.setattr("audio_memory.main.MacSecurityClient", object)
    monkeypatch.setattr(
        "audio_memory.main.ProviderStateCoordinator.initialize",
        initialize_provider,
    )
    monkeypatch.setattr(
        "audio_memory.main.AsrCredentialCoordinator.validate_saved",
        validate_asr,
    )
    monkeypatch.setattr(
        "audio_memory.main.ManagedStorageRuntime.ensure_ready",
        skip_managed_storage,
    )
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(
        runtime_config=runtime_config,
        **({"validate_connections_on_startup": False} if preview else {}),
    )

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert calls == ([("provider", False)] if preview else [("provider", True), ("asr", None), ("managed_storage", None)])


def test_production_rejects_skipping_connection_validation(tmp_path: Path) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "production"},
    )

    with pytest.raises(ValueError, match="development"):
        create_app(
            runtime_config=runtime_config,
            validate_connections_on_startup=False,
        )
