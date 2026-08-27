from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from audio_memory.api.providers import router
from audio_memory.providers.coordinator import ProviderStateCoordinator
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus
from audio_memory.providers.types import ValidationResult


class ConfiguredKeychain:
    def __init__(self) -> None:
        self.values = {
            "kimi": b"kimi-key",
            "deepseek": b"deepseek-key",
            "openai": b"openai-key",
            "glm": b"glm-key",
        }

    def read(self, provider_id: str) -> KeychainReadResult:
        return KeychainReadResult(KeychainStatus.CONFIGURED, self.values[provider_id])

    def replace(self, provider_id: str, candidate: bytes) -> None:
        self.values[provider_id] = candidate


class ModelRecordingValidator:
    def __init__(self) -> None:
        self.models: list[str] = []

    async def validate(self, secret: bytes) -> ValidationResult:
        return await self.validate_model(secret, model_id=None)

    async def validate_model(
        self, secret: bytes, *, model_id: str | None = None
    ) -> ValidationResult:
        assert secret
        self.models.append(model_id or "")
        return ValidationResult(ok=True)


@pytest.fixture
def model_app() -> FastAPI:
    app = FastAPI()
    keychain = ConfiguredKeychain()
    validators = {
        provider_id: ModelRecordingValidator() for provider_id in keychain.values
    }
    app.state.validators = validators
    app.state.provider_coordinator = ProviderStateCoordinator(
        keychain=keychain,
        validators=validators,
    )
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_provider_catalog_only_exposes_deepseek_v4_pro(model_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=model_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/providers")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert len(providers) == 1
    assert providers[0]["provider_id"] == "deepseek"
    assert providers[0]["model_id"] == "deepseek-v4-pro"
    assert providers[0]["model_options"] == [
        {"model_id": "deepseek-v4-pro", "label": "最高质量"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["kimi", "openai", "glm"])
async def test_retired_provider_configuration_endpoints_are_rejected_before_validation(
    model_app: FastAPI, provider_id: str
) -> None:
    transport = httpx.ASGITransport(app=model_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/api/providers/{provider_id}/key",
            headers={"X-Configuration-Session": "settings"},
            json={"api_key": "must-not-be-validated"},
        )

    assert response.status_code == 404
    assert model_app.state.validators[provider_id].models == []


@pytest.mark.asyncio
async def test_selecting_model_validates_and_updates_provider_snapshot(
    model_app: FastAPI,
) -> None:
    transport = httpx.ASGITransport(app=model_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/providers/deepseek/model", json={"model_id": "deepseek-v4-pro"}
        )

    assert response.status_code == 200
    assert response.json()["model_id"] == "deepseek-v4-pro"
    assert model_app.state.validators["deepseek"].models == ["deepseek-v4-pro"]
    snapshot = model_app.state.provider_coordinator.state("deepseek")
    assert snapshot.model_id == "deepseek-v4-pro"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "removed_model_id"),
    [
        ("deepseek", "deepseek-v4-flash"),
        ("kimi", "kimi-k2.6"),
    ],
)
async def test_removed_models_cannot_be_selected_for_new_work(
    model_app: FastAPI,
    provider_id: str,
    removed_model_id: str,
) -> None:
    transport = httpx.ASGITransport(app=model_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/api/providers/{provider_id}/model",
            json={"model_id": removed_model_id},
        )

    assert response.status_code == (422 if provider_id == "deepseek" else 404)
    assert model_app.state.validators[provider_id].models == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "removed_model_id"),
    [
        ("deepseek", "deepseek-v4-flash"),
        ("kimi", "kimi-k2.6"),
    ],
)
async def test_removed_models_cannot_be_submitted_while_saving_a_key(
    model_app: FastAPI,
    provider_id: str,
    removed_model_id: str,
) -> None:
    transport = httpx.ASGITransport(app=model_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/api/providers/{provider_id}/key",
            headers={"X-Configuration-Session": "removed-model"},
            json={"api_key": "new-key", "model_id": removed_model_id},
        )

    assert response.status_code == (422 if provider_id == "deepseek" else 404)
    assert response.json()["detail"] in {"Unsupported model", "Unsupported provider"}
    assert model_app.state.validators[provider_id].models == []


@pytest.mark.asyncio
async def test_provider_rejects_model_outside_curated_catalog(model_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=model_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/providers/glm/model", json={"model_id": "glm-user-entered"}
        )

    assert response.status_code == 404
    assert model_app.state.validators["glm"].models == []


@pytest.mark.asyncio
async def test_first_key_validation_uses_the_selected_model(model_app: FastAPI) -> None:
    model_app.state.provider_coordinator._keychain.values["deepseek"] = b"old-key"
    transport = httpx.ASGITransport(app=model_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/providers/deepseek/key",
            headers={"X-Configuration-Session": "settings"},
            json={"api_key": "new-key", "model_id": "deepseek-v4-pro"},
        )

    assert response.status_code == 200
    assert response.json()["model_id"] == "deepseek-v4-pro"
    assert model_app.state.validators["deepseek"].models == ["deepseek-v4-pro"]
