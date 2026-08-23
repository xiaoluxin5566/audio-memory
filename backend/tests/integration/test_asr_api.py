from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from audio_memory.api.asr import router
from audio_memory.asr.credentials import AsrCredentialCoordinator
from audio_memory.asr.types import AsrValidationResult
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class MemoryKeychain:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def read(self, credential_id: str) -> KeychainReadResult:
        if credential_id not in self.values:
            return KeychainReadResult(KeychainStatus.UNCONFIGURED)
        return KeychainReadResult(
            KeychainStatus.CONFIGURED, self.values[credential_id]
        )

    def replace(self, credential_id: str, candidate: bytes) -> None:
        self.values[credential_id] = candidate


class Validator:
    async def validate(self, candidate: bytes) -> AsrValidationResult:
        return AsrValidationResult(
            ok=candidate == b"secret-credential-7319",
            error_code=(
                None if candidate == b"secret-credential-7319" else "invalid_api_key"
            ),
        )


@pytest.fixture
def asr_app() -> FastAPI:
    app = FastAPI()
    app.state.asr_coordinator = AsrCredentialCoordinator(
        keychain=MemoryKeychain(), validator=Validator()
    )
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_asr_state_is_fixed_to_volcano_standard_model(asr_app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asr_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/asr")

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": "volcano",
        "display_name": "火山语音",
        "resource_id": "volc.seedasr.auc",
        "state": "unconfigured",
        "last_validated_at": None,
        "error_code": None,
    }


@pytest.mark.asyncio
async def test_asr_api_never_returns_candidate_key(asr_app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asr_app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/asr/key", json={"api_key": "secret-credential-7319"}
        )
        state = await client.get("/api/asr")

    assert response.status_code == 200
    assert response.json()["state"] == "available"
    assert "secret-credential-7319" not in response.text
    assert "api_key" not in state.text


@pytest.mark.asyncio
async def test_invalid_asr_key_is_an_actionable_stable_error(asr_app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asr_app), base_url="http://test"
    ) as client:
        response = await client.put("/api/asr/key", json={"api_key": "wrong"})

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "invalid_api_key"}
    assert "wrong" not in response.text
