from pathlib import Path

import httpx
import pytest

from audio_memory.config import (
    AppPaths,
    AppProfile,
    RuntimeConfig,
    RuntimeConfigurationError,
    UnsafeDevelopmentPathError,
)
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


def test_create_app_rejects_development_identity_with_production_paths_before_access(
    tmp_path: Path, fake_mac_security_client: list[FakeSecurityClient]
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    assert runtime_config.production_data_root is not None
    production_paths = AppPaths.from_roots(runtime_config.production_data_root)

    with pytest.raises(UnsafeDevelopmentPathError, match="正式数据目录重叠"):
        create_app(runtime_config=runtime_config, paths=production_paths)

    assert fake_mac_security_client == []
    assert not runtime_config.production_data_root.exists()
    assert not runtime_config.paths.root.exists()


def test_create_app_rejects_injected_opposite_keychain_namespace_before_access(
    tmp_path: Path, fake_mac_security_client: list[FakeSecurityClient]
) -> None:
    paths = AppPaths.from_roots(
        tmp_path / "project/.runtime/dev",
        tmp_path / "home/Library/Application Support/AudioMemory/models",
        models_writable=False,
    )
    runtime_config = RuntimeConfig(
        paths=paths,
        profile=AppProfile.DEVELOPMENT,
        port=8766,
        keychain_service="Audio Memory",
        production_data_root=(
            tmp_path / "home/Library/Application Support/AudioMemory"
        ),
    )

    with pytest.raises(RuntimeConfigurationError, match="Keychain service"):
        create_app(runtime_config=runtime_config)

    assert fake_mac_security_client == []
    assert not paths.root.exists()


def test_create_app_rejects_development_identity_without_a_production_boundary(
    tmp_path: Path, fake_mac_security_client: list[FakeSecurityClient]
) -> None:
    paths = AppPaths.from_roots(
        tmp_path / "project/.runtime/dev",
        tmp_path / "shared/models",
        models_writable=False,
    )
    runtime_config = RuntimeConfig(
        paths=paths,
        profile=AppProfile.DEVELOPMENT,
        port=8766,
        keychain_service="Audio Memory Dev",
    )

    with pytest.raises(RuntimeConfigurationError, match="正式数据目录边界"):
        create_app(runtime_config=runtime_config)

    assert fake_mac_security_client == []
    assert not paths.root.exists()


@pytest.mark.asyncio
async def test_create_app_uses_one_validated_effective_runtime_for_overrides(
    tmp_path: Path,
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    override_paths = AppPaths.from_roots(
        tmp_path / "override-data",
        runtime_config.paths.models,
        models_writable=False,
    )

    app = create_app(
        runtime_config=runtime_config,
        paths=override_paths,
        local_port=9123,
    )

    assert app.state.runtime_config.paths is override_paths
    assert app.state.runtime_config.port == 9123
    assert app.state.runtime_config.profile is AppProfile.DEVELOPMENT
    async with app.router.lifespan_context(app):
        assert app.state.paths is override_paths
        assert override_paths.database.is_file()
    assert not runtime_config.paths.root.exists()


@pytest.mark.asyncio
async def test_development_lifespan_revalidates_paths_immediately_before_writes(
    tmp_path: Path,
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime_config.paths.root.mkdir(parents=True)
    runtime_config.paths.runtime.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeDevelopmentPathError, match="派生可写路径"):
        async with app.router.lifespan_context(app):
            pass

    assert not runtime_config.paths.database.exists()
    assert not any(outside.iterdir())


@pytest.mark.asyncio
async def test_startup_migrates_and_opens_the_local_database(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)
    app = create_app(paths=paths)

    async with app.router.lifespan_context(app):
        assert paths.database.is_file()
        assert app.state.database.path == paths.database

    assert app.state.database.engine.sync_engine.pool.checkedout() == 0
