from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from audio_memory.api.providers import router
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus
from audio_memory.providers.types import ProviderStateName, ValidationResult


class MemoryKeychain:
    def __init__(self) -> None:
        self.values = {"kimi": None, "deepseek": None, "openai": None, "glm": None}

    def read(self, provider_id: str):
        secret = self.values[provider_id]
        if secret is None:
            return KeychainReadResult(KeychainStatus.UNCONFIGURED)
        return KeychainReadResult(KeychainStatus.CONFIGURED, secret)

    def replace(self, provider_id: str, candidate: bytes) -> None:
        self.values[provider_id] = candidate


class AlwaysValid:
    def __init__(self) -> None:
        self.calls = 0

    async def validate(self, secret: bytes) -> ValidationResult:
        self.calls += 1
        return ValidationResult(True)


@pytest.fixture
def provider_app() -> FastAPI:
    app = FastAPI()
    keychain = MemoryKeychain()
    validators = {provider_id: AlwaysValid() for provider_id in keychain.values}
    app.state.keychain = keychain
    app.state.validators = validators
    app.state.provider_coordinator = ProviderStateCoordinator(
        keychain=keychain,
        validators=validators,
    )
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_validate_configured_only_calls_keychain_configured_providers(
    provider_app: FastAPI,
) -> None:
    provider_app.state.keychain.values["deepseek"] = b"saved-key"
    validators = provider_app.state.validators
    transport = httpx.ASGITransport(app=provider_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/providers/validate-configured")

    assert response.status_code == 200
    states = {item["provider_id"]: item["state"] for item in response.json()["providers"]}
    assert states["deepseek"] == "available"
    assert validators["deepseek"].calls == 1
    assert validators["kimi"].calls == 0


@pytest.mark.asyncio
async def test_provider_responses_never_include_keys(provider_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=provider_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved = await client.put(
            "/api/providers/deepseek/key",
            headers={"X-Configuration-Session": "window-a"},
            json={"api_key": "top-secret-value"},
        )
        listed = await client.get("/api/providers")

    assert saved.status_code == 200
    assert "top-secret-value" not in saved.text
    assert "top-secret-value" not in listed.text
    assert "api_key" not in listed.text


@pytest.mark.asyncio
async def test_saving_key_does_not_auto_activate(provider_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=provider_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/providers/deepseek/key",
            headers={"X-Configuration-Session": "window-a"},
            json={"api_key": "candidate"},
        )
        listed = (await client.get("/api/providers")).json()

    deepseek = next(
        item for item in listed["providers"] if item["provider_id"] == "deepseek"
    )
    assert deepseek["state"] == "available"
    assert deepseek["active"] is False


@pytest.mark.asyncio
async def test_unavailable_provider_cannot_be_activated(provider_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=provider_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/providers/deepseek/activate")

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_available_provider_activation_is_idempotent(provider_app: FastAPI) -> None:
    coordinator = provider_app.state.provider_coordinator
    coordinator.set_state_for_test("deepseek", ProviderStateName.AVAILABLE)
    transport = httpx.ASGITransport(app=provider_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/providers/deepseek/activate")
        second = await client.post("/api/providers/deepseek/activate")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["active"] is True
