from pathlib import Path

import httpx
import pytest

from audio_memory.config import AppPaths, RuntimeConfig
from audio_memory.main import create_app
from audio_memory.providers.keychain import ERR_SEC_ITEM_NOT_FOUND


class FakeSecurityClient:
    def __init__(self) -> None:
        self.service_accounts: list[tuple[str, str]] = []

    def read(self, service: str, account: str) -> tuple[int, None]:
        self.service_accounts.append((service, account))
        return ERR_SEC_ITEM_NOT_FOUND, None

    def update(self, service: str, account: str, value: bytes) -> int:
        self.service_accounts.append((service, account))
        return ERR_SEC_ITEM_NOT_FOUND

    def add(self, service: str, account: str, value: bytes) -> int:
        self.service_accounts.append((service, account))
        return ERR_SEC_ITEM_NOT_FOUND


@pytest.fixture(autouse=True)
def fake_mac_security_client(
    monkeypatch: pytest.MonkeyPatch,
) -> list[FakeSecurityClient]:
    clients: list[FakeSecurityClient] = []

    def create_client() -> FakeSecurityClient:
        client = FakeSecurityClient()
        clients.append(client)
        return client

    monkeypatch.setattr("audio_memory.main.MacSecurityClient", create_client)
    return clients


@pytest.mark.asyncio
async def test_health_is_ready_after_application_startup(tmp_path: Path) -> None:
    app = create_app(paths=AppPaths.from_home(tmp_path))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
        ) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0-beta.1",
        "platform": "macOS",
        "architecture": "arm64",
        "profile": "production",
    }


@pytest.mark.asyncio
async def test_health_exposes_development_profile_without_runtime_details(
    tmp_path: Path, fake_mac_security_client: list[FakeSecurityClient]
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8766",
        ) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["profile"] == "development"
    assert {"data_root", "model_root", "keychain_service"}.isdisjoint(
        response.json()
    )
    assert fake_mac_security_client[0].service_accounts == [
        ("Audio Memory Dev", "provider:kimi"),
        ("Audio Memory Dev", "provider:deepseek"),
        ("Audio Memory Dev", "provider:openai"),
        ("Audio Memory Dev", "provider:glm"),
    ]


@pytest.mark.asyncio
async def test_startup_migrates_and_opens_the_local_database(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)
    app = create_app(paths=paths)

    async with app.router.lifespan_context(app):
        assert paths.database.is_file()
        assert app.state.database.path == paths.database

    assert app.state.database.engine.sync_engine.pool.checkedout() == 0
