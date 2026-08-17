from __future__ import annotations

import asyncio

import pytest

from audio_memory.db import Database
from audio_memory.repositories import ProviderMetadataRepository
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus
from audio_memory.providers.types import (
    ProviderStateName,
    ValidationErrorCode,
    ValidationResult,
)


class FakeKeychain:
    def __init__(self) -> None:
        self.values = {
            "kimi": b"saved",
            "deepseek": None,
            "openai": None,
            "glm": None,
        }

    def read(self, provider_id: str) -> KeychainReadResult:
        value = self.values[provider_id]
        if value is None:
            return KeychainReadResult(KeychainStatus.UNCONFIGURED)
        return KeychainReadResult(KeychainStatus.CONFIGURED, value)

    def replace(self, provider_id: str, candidate: bytes) -> None:
        self.values[provider_id] = candidate


class BlockingValidator:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def validate(self, secret: bytes) -> ValidationResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return ValidationResult(ok=True)


class AcceptingValidator:
    async def validate(self, secret: bytes) -> ValidationResult:
        return ValidationResult(ok=True)


class RecordingMetadata:
    def __init__(self, events: list[str], *, fail_generation: bool = False) -> None:
        self.events = events
        self.fail_generation = fail_generation
        self.generation = 0

    async def update_generation(self, provider_id: str, generation: int) -> None:
        self.events.append(f"durable:{generation}")
        if self.fail_generation:
            raise RuntimeError("generation persistence failed")
        self.generation = generation

    async def update_validation(self, provider_id: str, **kwargs) -> None:
        self.events.append("validation")

    async def update_model(self, provider_id: str, model_id: str) -> None:
        self.events.append(f"model:{model_id}")


@pytest.mark.asyncio
async def test_same_provider_validation_is_deduplicated() -> None:
    keychain = FakeKeychain()
    validator = BlockingValidator()
    coordinator = ProviderStateCoordinator(
        keychain=keychain, validators={"kimi": validator}
    )

    first = asyncio.create_task(coordinator.validate_saved("kimi"))
    await validator.started.wait()
    second = asyncio.create_task(coordinator.validate_saved("kimi"))
    await asyncio.sleep(0)
    validator.release.set()

    assert (await first).ok
    assert (await second).ok
    assert validator.calls == 1
    assert coordinator.state("kimi").state is ProviderStateName.AVAILABLE


@pytest.mark.asyncio
async def test_candidate_is_session_scoped_and_does_not_change_formal_state() -> None:
    keychain = FakeKeychain()
    validator = BlockingValidator()
    coordinator = ProviderStateCoordinator(
        keychain=keychain, validators={"kimi": validator}
    )
    coordinator.set_state_for_test("kimi", ProviderStateName.AVAILABLE)

    task = asyncio.create_task(
        coordinator.validate_candidate("kimi", "window-a", b"candidate")
    )
    await validator.started.wait()
    assert coordinator.state("kimi").state is ProviderStateName.AVAILABLE

    await coordinator.cancel_candidate("kimi", "window-a")
    validator.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.state("kimi").state is ProviderStateName.AVAILABLE
    assert keychain.values["kimi"] == b"saved"


@pytest.mark.asyncio
async def test_stale_saved_validation_cannot_overwrite_new_generation() -> None:
    keychain = FakeKeychain()
    validator = BlockingValidator()
    coordinator = ProviderStateCoordinator(
        keychain=keychain, validators={"kimi": validator}
    )

    task = asyncio.create_task(coordinator.validate_saved("kimi"))
    await validator.started.wait()
    coordinator.bump_generation_for_test("kimi")
    coordinator.set_state_for_test("kimi", ProviderStateName.UNAVAILABLE)
    validator.release.set()
    await task

    assert coordinator.state("kimi").state is ProviderStateName.UNAVAILABLE


@pytest.mark.asyncio
async def test_unconfigured_provider_never_calls_network() -> None:
    keychain = FakeKeychain()
    validator = BlockingValidator()
    coordinator = ProviderStateCoordinator(
        keychain=keychain, validators={"deepseek": validator}
    )

    result = await coordinator.validate_saved("deepseek")

    assert result.ok is False
    assert validator.calls == 0
    assert coordinator.state("deepseek").state is ProviderStateName.UNCONFIGURED


@pytest.mark.asyncio
async def test_rate_limit_cooldown_prevents_retry_storm() -> None:
    class RateLimitedValidator:
        calls = 0

        async def validate(self, secret: bytes) -> ValidationResult:
            self.calls += 1
            return ValidationResult(
                False,
                ValidationErrorCode.RATE_LIMITED,
                retry_after_seconds=60,
            )

    validator = RateLimitedValidator()
    coordinator = ProviderStateCoordinator(
        keychain=FakeKeychain(), validators={"kimi": validator}
    )

    await coordinator.validate_saved("kimi")
    second = await coordinator.validate_saved("kimi")

    assert validator.calls == 1
    assert second.error_code is ValidationErrorCode.RATE_LIMITED
    assert coordinator.state("kimi").cooldown_until is not None


@pytest.mark.asyncio
async def test_active_snapshot_includes_credential_generation_atomically() -> None:
    class AcceptingValidator:
        async def validate(self, secret: bytes) -> ValidationResult:
            return ValidationResult(ok=True)

    coordinator = ProviderStateCoordinator(
        keychain=FakeKeychain(), validators={"kimi": AcceptingValidator()}
    )
    coordinator.set_state_for_test("kimi", ProviderStateName.AVAILABLE)
    await coordinator.activate("kimi")

    first_state, first_generation = (
        await coordinator.snapshot_active_with_generation()
    )
    await coordinator.validate_candidate("kimi", "settings", b"replacement")
    second_state, second_generation = (
        await coordinator.snapshot_active_with_generation()
    )

    assert first_state.provider_id == second_state.provider_id == "kimi"
    assert first_generation == 0
    assert second_generation == 1


@pytest.mark.asyncio
async def test_credential_generation_survives_coordinator_restart(tmp_path) -> None:
    class AcceptingValidator:
        async def validate(self, secret: bytes) -> ValidationResult:
            return ValidationResult(ok=True)

    database = Database(tmp_path / "provider-generation.sqlite3")
    await database.create_schema()
    keychain = FakeKeychain()
    validators = {"kimi": AcceptingValidator()}
    first = ProviderStateCoordinator(
        keychain=keychain,
        validators=validators,
        metadata=ProviderMetadataRepository(database),
    )
    await first.initialize()
    await first.activate("kimi")
    await first.validate_candidate("kimi", "settings", b"replacement")

    restarted = ProviderStateCoordinator(
        keychain=keychain,
        validators=validators,
        metadata=ProviderMetadataRepository(database),
    )
    await restarted.initialize()
    provider, generation = await restarted.snapshot_active_with_generation()

    assert provider.provider_id == "kimi"
    assert generation == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_publication_guard_blocks_physical_credential_replacement() -> None:
    class AcceptingValidator:
        async def validate(self, secret: bytes) -> ValidationResult:
            return ValidationResult(ok=True)

    class GuardedKeychain(FakeKeychain):
        def __init__(self) -> None:
            super().__init__()
            self.replace_called = asyncio.Event()

        def replace(self, provider_id: str, candidate: bytes) -> None:
            self.replace_called.set()
            super().replace(provider_id, candidate)

    keychain = GuardedKeychain()
    coordinator = ProviderStateCoordinator(
        keychain=keychain,
        validators={"kimi": AcceptingValidator()},
    )

    async with coordinator.publication_guard("kimi"):
        replacement = asyncio.create_task(
            coordinator.validate_candidate("kimi", "settings", b"replacement")
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(keychain.replace_called.wait(), timeout=0.05)
        assert keychain.values["kimi"] == b"saved"

    assert (await replacement).ok
    assert keychain.values["kimi"] == b"replacement"


@pytest.mark.asyncio
async def test_generation_is_durable_before_physical_credential_replacement() -> None:
    events: list[str] = []
    metadata = RecordingMetadata(events)

    class OrderedKeychain(FakeKeychain):
        def replace(self, provider_id: str, candidate: bytes) -> None:
            events.append("physical")
            super().replace(provider_id, candidate)

    coordinator = ProviderStateCoordinator(
        keychain=OrderedKeychain(),
        validators={"kimi": AcceptingValidator()},
        metadata=metadata,
    )

    await coordinator.validate_candidate("kimi", "settings", b"replacement")

    assert events[:2] == ["durable:1", "physical"]
    assert metadata.generation == 1


@pytest.mark.asyncio
async def test_generation_persistence_failure_keeps_the_old_physical_key() -> None:
    events: list[str] = []
    keychain = FakeKeychain()
    coordinator = ProviderStateCoordinator(
        keychain=keychain,
        validators={"kimi": AcceptingValidator()},
        metadata=RecordingMetadata(events, fail_generation=True),
    )

    with pytest.raises(RuntimeError, match="persistence"):
        await coordinator.validate_candidate("kimi", "settings", b"replacement")

    assert keychain.values["kimi"] == b"saved"
    assert await coordinator.credential_generation("kimi") == 0
    assert events == ["durable:1"]


@pytest.mark.asyncio
async def test_physical_replacement_failure_keeps_the_incremented_generation() -> None:
    events: list[str] = []
    metadata = RecordingMetadata(events)

    class FailingKeychain(FakeKeychain):
        def replace(self, provider_id: str, candidate: bytes) -> None:
            events.append("physical")
            raise RuntimeError("keychain replacement failed")

    keychain = FailingKeychain()
    coordinator = ProviderStateCoordinator(
        keychain=keychain,
        validators={"kimi": AcceptingValidator()},
        metadata=metadata,
    )

    with pytest.raises(RuntimeError, match="replacement"):
        await coordinator.validate_candidate("kimi", "settings", b"replacement")

    assert events == ["durable:1", "physical"]
    assert metadata.generation == 1
    assert await coordinator.credential_generation("kimi") == 1
    assert keychain.values["kimi"] == b"saved"


@pytest.mark.asyncio
async def test_failed_confirmation_cannot_leave_new_key_on_old_generation() -> None:
    events: list[str] = []
    metadata = RecordingMetadata(events)

    class UnconfirmedKeychain(FakeKeychain):
        replaced = False

        def replace(self, provider_id: str, candidate: bytes) -> None:
            events.append("physical")
            super().replace(provider_id, candidate)
            self.replaced = True

        def read(self, provider_id: str) -> KeychainReadResult:
            if self.replaced:
                return KeychainReadResult(KeychainStatus.UNAVAILABLE)
            return super().read(provider_id)

    keychain = UnconfirmedKeychain()
    coordinator = ProviderStateCoordinator(
        keychain=keychain,
        validators={"kimi": AcceptingValidator()},
        metadata=metadata,
    )

    result = await coordinator.validate_candidate(
        "kimi", "settings", b"replacement"
    )

    assert result.error_code is ValidationErrorCode.KEYCHAIN_UNAVAILABLE
    assert events[:2] == ["durable:1", "physical"]
    assert metadata.generation == 1
    assert await coordinator.credential_generation("kimi") == 1
    assert keychain.values["kimi"] == b"replacement"
