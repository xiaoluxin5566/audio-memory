from __future__ import annotations

import asyncio

import pytest

from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus
from audio_memory.providers.types import (
    ProviderStateName,
    ValidationErrorCode,
    ValidationResult,
)


class FakeKeychain:
    def __init__(self) -> None:
        self.values = {"kimi": b"saved", "deepseek": None, "openai": None}

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

