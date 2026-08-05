from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from audio_memory.providers.keychain import KeychainRepository, KeychainStatus
from audio_memory.providers.types import (
    PROVIDER_CONFIGS,
    ProviderState,
    ProviderStateName,
    ValidationErrorCode,
    ValidationResult,
)


class Validator(Protocol):
    async def validate(self, secret: bytes) -> ValidationResult: ...


class MetadataStore(Protocol):
    async def ensure_defaults(self, model_ids: dict[str, str]) -> None: ...

    async def list_all(self) -> list[object]: ...

    async def activate(self, provider_id: str) -> None: ...

    async def update_validation(
        self,
        provider_id: str,
        *,
        status: str,
        validated_at: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> None: ...


class ProviderStateCoordinator:
    def __init__(
        self,
        *,
        keychain: KeychainRepository,
        validators: Mapping[str, Validator],
        metadata: MetadataStore | None = None,
    ) -> None:
        self._keychain = keychain
        self._validators = dict(validators)
        self._metadata = metadata
        self._states = {
            provider_id: ProviderState(
                provider_id=provider_id,
                display_name=config.display_name,
                model_id=config.model_id,
            )
            for provider_id, config in PROVIDER_CONFIGS.items()
        }
        self._generations = {provider_id: 0 for provider_id in PROVIDER_CONFIGS}
        self._inflight: dict[tuple[str, int], asyncio.Task[ValidationResult]] = {}
        self._candidates: dict[tuple[str, str], tuple[str, asyncio.Task[ValidationResult]]] = {}
        self._state_lock = asyncio.Lock()
        self._activation_lock = asyncio.Lock()
        self._write_locks = {
            provider_id: asyncio.Lock() for provider_id in PROVIDER_CONFIGS
        }
        self._cooldown_deadlines: dict[tuple[str, int], float] = {}

    def list_states(self) -> list[ProviderState]:
        return list(self._states.values())

    def state(self, provider_id: str) -> ProviderState:
        return self._states[provider_id]

    async def initialize(self) -> None:
        if self._metadata is not None:
            await self._metadata.ensure_defaults(
                {
                    provider_id: config.model_id
                    for provider_id, config in PROVIDER_CONFIGS.items()
                }
            )
            for row in await self._metadata.list_all():
                if getattr(row, "active", False):
                    self._set_active(str(getattr(row, "provider_id")))
        await asyncio.gather(
            *(self.validate_saved(provider_id) for provider_id in self._states)
        )

    async def activate(self, provider_id: str) -> ProviderState:
        async with self._activation_lock:
            target = self._states[provider_id]
            if target.state is not ProviderStateName.AVAILABLE:
                raise ValueError("Only an available provider can be activated")
            if target.active:
                return target
            if self._metadata is not None:
                await self._metadata.activate(provider_id)
            self._set_active(provider_id)
            return self._states[provider_id]

    async def snapshot_active(self) -> ProviderState:
        async with self._activation_lock:
            active = next((item for item in self._states.values() if item.active), None)
            if active is None:
                raise LookupError("No active provider")
            return active

    def _set_active(self, provider_id: str) -> None:
        for item_id, item in tuple(self._states.items()):
            self._states[item_id] = ProviderState(
                provider_id=item.provider_id,
                display_name=item.display_name,
                model_id=item.model_id,
                active=item_id == provider_id,
                state=item.state,
                last_validated_at=item.last_validated_at,
                error_code=item.error_code,
                error_message=item.error_message,
                cooldown_until=item.cooldown_until,
            )

    async def validate_saved(self, provider_id: str) -> ValidationResult:
        generation = self._generations[provider_id]
        key = (provider_id, generation)
        async with self._state_lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._validate_saved_once(provider_id, generation)
                )
                self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._state_lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _validate_saved_once(
        self, provider_id: str, generation: int
    ) -> ValidationResult:
        cooldown_key = (provider_id, generation)
        deadline = self._cooldown_deadlines.get(cooldown_key)
        if deadline is not None and time.monotonic() < deadline:
            remaining = max(1, int(deadline - time.monotonic()))
            return ValidationResult(
                False,
                ValidationErrorCode.RATE_LIMITED,
                "请求过于频繁，请稍后重试",
                remaining,
            )
        read = self._keychain.read(provider_id)
        if read.status is KeychainStatus.UNCONFIGURED:
            result = ValidationResult(False, ValidationErrorCode.INVALID_KEY)
            await self._apply_if_current(
                provider_id, generation, ProviderStateName.UNCONFIGURED, result
            )
            return result
        if read.status is KeychainStatus.UNAVAILABLE or read.secret is None:
            result = ValidationResult(False, ValidationErrorCode.KEYCHAIN_UNAVAILABLE)
            await self._apply_if_current(
                provider_id, generation, ProviderStateName.KEYCHAIN_UNAVAILABLE, result
            )
            return result

        self._set_state(provider_id, ProviderStateName.VALIDATING)
        result = await self._validators[provider_id].validate(read.secret)
        if (
            result.error_code is ValidationErrorCode.RATE_LIMITED
            and result.retry_after_seconds
        ):
            self._cooldown_deadlines[cooldown_key] = (
                time.monotonic() + result.retry_after_seconds
            )
        target = (
            ProviderStateName.AVAILABLE if result.ok else ProviderStateName.UNAVAILABLE
        )
        await self._apply_if_current(provider_id, generation, target, result)
        return result

    async def validate_candidate(
        self, provider_id: str, session_id: str, candidate: bytes
    ) -> ValidationResult:
        candidate_id = str(uuid4())
        task = asyncio.create_task(self._validators[provider_id].validate(candidate))
        key = (provider_id, session_id)
        prior = self._candidates.get(key)
        if prior is not None:
            prior[1].cancel()
        self._candidates[key] = (candidate_id, task)
        try:
            result = await task
            current = self._candidates.get(key)
            if current is None or current[0] != candidate_id:
                raise asyncio.CancelledError
            if not result.ok:
                return result
            async with self._write_locks[provider_id]:
                current = self._candidates.get(key)
                if current is None or current[0] != candidate_id:
                    raise asyncio.CancelledError
                self._keychain.replace(provider_id, candidate)
                confirmed = self._keychain.read(provider_id)
                if confirmed.status is not KeychainStatus.CONFIGURED:
                    return ValidationResult(
                        False, ValidationErrorCode.KEYCHAIN_UNAVAILABLE
                    )
                self._generations[provider_id] += 1
                self._set_state(provider_id, ProviderStateName.AVAILABLE)
                await self._persist_state(provider_id)
            return result
        finally:
            current = self._candidates.get(key)
            if current is not None and current[0] == candidate_id:
                self._candidates.pop(key, None)

    async def cancel_candidate(self, provider_id: str, session_id: str) -> None:
        current = self._candidates.pop((provider_id, session_id), None)
        if current is not None:
            current[1].cancel()

    async def _apply_if_current(
        self,
        provider_id: str,
        generation: int,
        state: ProviderStateName,
        result: ValidationResult,
    ) -> None:
        if self._generations[provider_id] != generation:
            return
        self._set_state(
            provider_id,
            state,
            error_code=result.error_code,
            error_message=result.message,
            validated=result.ok,
        )
        await self._persist_state(provider_id)

    async def _persist_state(self, provider_id: str) -> None:
        if self._metadata is None:
            return
        state = self._states[provider_id]
        await self._metadata.update_validation(
            provider_id,
            status=state.state.value,
            validated_at=(
                state.last_validated_at.isoformat() if state.last_validated_at else None
            ),
            error_code=state.error_code.value if state.error_code else None,
            error_message=state.error_message,
        )

    def _set_state(
        self,
        provider_id: str,
        state: ProviderStateName,
        *,
        error_code: ValidationErrorCode | None = None,
        error_message: str | None = None,
        validated: bool = False,
    ) -> None:
        old = self._states[provider_id]
        cooldown_until = old.cooldown_until
        if (
            error_code is ValidationErrorCode.RATE_LIMITED
            and provider_id in self._generations
        ):
            deadline = self._cooldown_deadlines.get(
                (provider_id, self._generations[provider_id])
            )
            if deadline is not None:
                cooldown_until = datetime.now(UTC) + timedelta(
                    seconds=max(0, deadline - time.monotonic())
                )
        self._states[provider_id] = ProviderState(
            provider_id=old.provider_id,
            display_name=old.display_name,
            model_id=old.model_id,
            active=old.active,
            state=state,
            last_validated_at=datetime.now(UTC) if validated else old.last_validated_at,
            error_code=error_code,
            error_message=error_message,
            cooldown_until=cooldown_until,
        )

    def set_state_for_test(self, provider_id: str, state: ProviderStateName) -> None:
        self._set_state(provider_id, state)

    def bump_generation_for_test(self, provider_id: str) -> None:
        self._generations[provider_id] += 1
