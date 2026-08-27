from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from audio_memory.providers.keychain import KeychainRepository, KeychainStatus
from audio_memory.providers.types import (
    CONFIGURABLE_PROVIDER_IDS,
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

    async def update_generation(self, provider_id: str, generation: int) -> None: ...

    async def update_model(self, provider_id: str, model_id: str) -> None: ...


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
                provider_id = str(getattr(row, "provider_id"))
                stored_model = str(getattr(row, "default_model_id", "") or "")
                if PROVIDER_CONFIGS[provider_id].supports_model(stored_model):
                    self._set_model(provider_id, stored_model)
                self._generations[provider_id] = int(
                    getattr(row, "credential_generation", 0)
                )
                if (
                    getattr(row, "active", False)
                    and provider_id in CONFIGURABLE_PROVIDER_IDS
                ):
                    self._set_active(provider_id)
        await asyncio.gather(
            *(
                self.validate_saved(provider_id)
                for provider_id in CONFIGURABLE_PROVIDER_IDS
            )
        )
        if not any(item.active for item in self._states.values()):
            fallback = next(
                (
                    provider_id
                    for provider_id in CONFIGURABLE_PROVIDER_IDS
                    if self._states[provider_id].state is ProviderStateName.AVAILABLE
                ),
                None,
            )
            if fallback is not None:
                if self._metadata is not None:
                    await self._metadata.activate(fallback)
                self._set_active(fallback)

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

    async def select_model(self, provider_id: str, model_id: str) -> ProviderState:
        config = PROVIDER_CONFIGS[provider_id]
        if not config.supports_model(model_id):
            raise ValueError("Unsupported model")
        read = self._keychain.read(provider_id)
        if read.status is not KeychainStatus.CONFIGURED or read.secret is None:
            raise LookupError("Configure this provider before selecting a model")
        validator = self._validators[provider_id]
        validate_model = getattr(validator, "validate_model", None)
        result = (
            await validate_model(read.secret, model_id=model_id)
            if validate_model is not None
            else await validator.validate(read.secret)
        )
        if not result.ok:
            raise ValueError(result.message or "Selected model validation failed")
        async with self._state_lock:
            if self._metadata is not None:
                await self._metadata.update_model(provider_id, model_id)
            self._set_model(provider_id, model_id)
            self._set_state(provider_id, ProviderStateName.AVAILABLE, validated=True)
        await self._persist_state(provider_id)
        return self._states[provider_id]

    async def snapshot_active(self) -> ProviderState:
        active, _generation = await self.snapshot_active_with_generation()
        return active

    async def snapshot_active_with_generation(self) -> tuple[ProviderState, int]:
        async with self._activation_lock:
            async with self._state_lock:
                active = next(
                    (item for item in self._states.values() if item.active), None
                )
                if active is None:
                    raise LookupError("No active provider")
                return active, self._generations[active.provider_id]

    @asynccontextmanager
    async def active_snapshot_guard(self):
        """Freeze active provider/model/generation across paid-work creation."""
        async with self._activation_lock:
            async with self._state_lock:
                active = next(
                    (item for item in self._states.values() if item.active), None
                )
                if active is None:
                    raise LookupError("No active provider")
                yield active, self._generations[active.provider_id]

    async def credential_generation(self, provider_id: str) -> int:
        async with self._state_lock:
            return self._generations[provider_id]

    @asynccontextmanager
    async def publication_guard(self, provider_id: str):
        async with self._state_lock:
            yield self._generations[provider_id]

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

    def _set_model(self, provider_id: str, model_id: str) -> None:
        item = self._states[provider_id]
        self._states[provider_id] = ProviderState(
            provider_id=item.provider_id,
            display_name=item.display_name,
            model_id=model_id,
            active=item.active,
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
        self,
        provider_id: str,
        session_id: str,
        candidate: bytes,
        *,
        model_id: str | None = None,
    ) -> ValidationResult:
        selected_model = model_id or self._states[provider_id].model_id
        if not PROVIDER_CONFIGS[provider_id].supports_model(selected_model):
            raise ValueError("Unsupported model")
        candidate_id = str(uuid4())
        validator = self._validators[provider_id]
        validate_model = getattr(validator, "validate_model", None)
        task = asyncio.create_task(
            validate_model(candidate, model_id=selected_model)
            if validate_model is not None
            else validator.validate(candidate)
        )
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
                async with self._state_lock:
                    current = self._candidates.get(key)
                    if current is None or current[0] != candidate_id:
                        raise asyncio.CancelledError
                    generation = self._generations[provider_id] + 1
                    if self._metadata is not None:
                        await self._metadata.update_generation(
                            provider_id, generation
                        )
                    self._generations[provider_id] = generation
                    self._keychain.replace(provider_id, candidate)
                    confirmed = self._keychain.read(provider_id)
                    if confirmed.status is not KeychainStatus.CONFIGURED:
                        replacement_result = ValidationResult(
                            False,
                            ValidationErrorCode.KEYCHAIN_UNAVAILABLE,
                        )
                        self._set_state(
                            provider_id,
                            ProviderStateName.KEYCHAIN_UNAVAILABLE,
                            error_code=ValidationErrorCode.KEYCHAIN_UNAVAILABLE,
                        )
                    else:
                        replacement_result = result
                        if self._metadata is not None:
                            await self._metadata.update_model(
                                provider_id, selected_model
                            )
                        self._set_model(provider_id, selected_model)
                        self._set_state(provider_id, ProviderStateName.AVAILABLE)
                await self._persist_state(provider_id)
            return replacement_result
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
