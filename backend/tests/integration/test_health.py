import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from audio_memory.config import (
    AppPaths,
    AppProfile,
    RuntimeConfig,
    RuntimeConfigurationError,
    UnsafeDevelopmentPathError,
)
from audio_memory.main import create_app
from audio_memory.instance_lock import InstanceLock
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    Transcript,
)
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
        "version": "0.1.0-beta.2",
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
async def test_development_lifespan_rejects_data_root_symlink_swap_without_target_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    effective_config = app.state.runtime_config
    data_root = effective_config.paths.root
    data_root.parent.mkdir(parents=True)
    protected_target = tmp_path / "production-target"
    protected_target.mkdir()
    original_validate = RuntimeConfig.validate
    swapped = False

    def validate_then_swap(config: RuntimeConfig) -> None:
        nonlocal swapped
        original_validate(config)
        if config is effective_config and not swapped:
            data_root.symlink_to(protected_target, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(RuntimeConfig, "validate", validate_then_swap)

    with pytest.raises(UnsafeDevelopmentPathError):
        async with app.router.lifespan_context(app):
            pass

    assert swapped is True
    assert not any(protected_target.iterdir())


@pytest.mark.asyncio
async def test_development_lifespan_rejects_database_hardlink_added_after_app_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    effective_config = app.state.runtime_config
    development_root = effective_config.paths.root
    development_root.mkdir(parents=True)
    protected_database = tmp_path / "production-audio-memory.sqlite3"
    protected_database.write_bytes(b"")
    original_validate = RuntimeConfig.validate
    linked = False

    def validate_then_link(config: RuntimeConfig) -> None:
        nonlocal linked
        original_validate(config)
        if config is effective_config and not linked:
            os.link(protected_database, effective_config.paths.database)
            linked = True

    monkeypatch.setattr(RuntimeConfig, "validate", validate_then_link)

    with pytest.raises(UnsafeDevelopmentPathError, match="硬链接"):
        async with app.router.lifespan_context(app):
            pass

    assert linked is True
    assert protected_database.read_bytes() == b""
    assert not effective_config.paths.runtime.exists()


@pytest.mark.asyncio
async def test_development_lock_is_descriptor_anchored_during_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    data_root = app.state.runtime_config.paths.root
    parked_root = data_root.with_name("dev-before-lock-swap")
    protected_target = tmp_path / "production-target"
    protected_target.mkdir()
    original_acquire = InstanceLock.acquire
    swapped = False

    def swap_then_acquire(lock: InstanceLock) -> None:
        nonlocal swapped
        if not swapped:
            data_root.rename(parked_root)
            data_root.symlink_to(protected_target, target_is_directory=True)
            swapped = True
        original_acquire(lock)

    monkeypatch.setattr(InstanceLock, "acquire", swap_then_acquire)

    with pytest.raises(UnsafeDevelopmentPathError):
        async with app.router.lifespan_context(app):
            pass

    assert swapped is True
    assert not any(protected_target.iterdir())


@pytest.mark.asyncio
async def test_development_migration_rejects_hardlink_inserted_at_call_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    database_path = app.state.runtime_config.paths.database
    protected_database = tmp_path / "production-audio-memory.sqlite3"
    protected_database.write_bytes(b"")
    from audio_memory import main as main_module

    original_migrate = main_module.run_migrations
    linked = False

    def link_then_migrate(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal linked
        os.link(protected_database, path)
        linked = True
        original_migrate(path, *args, **kwargs)

    monkeypatch.setattr(main_module, "run_migrations", link_then_migrate)

    with pytest.raises(UnsafeDevelopmentPathError, match="硬链接"):
        async with app.router.lifespan_context(app):
            pass

    assert linked is True
    assert protected_database.read_bytes() == b""


@pytest.mark.asyncio
async def test_development_session_rejects_hardlink_inserted_after_startup(
    tmp_path: Path,
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    session_path = app.state.runtime_config.paths.local_session
    protected_database = tmp_path / "production-session.sqlite3"
    protected_database.write_bytes(b"")

    async with app.router.lifespan_context(app):
        os.link(protected_database, session_path)
        with pytest.raises(UnsafeDevelopmentPathError, match="硬链接"):
            app.state.local_web_security.issue_session()

    assert protected_database.read_bytes() == b""


@pytest.mark.asyncio
async def test_development_database_rechecks_reused_connection_before_sql(
    tmp_path: Path,
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    database_path = app.state.runtime_config.paths.database
    protected_database = tmp_path / "production-audio-memory.sqlite3"

    async with app.router.lifespan_context(app):
        async with app.state.database.session() as session:
            await session.execute(text("SELECT 1"))
            os.link(database_path, protected_database)
            protected_before = protected_database.read_bytes()
            try:
                with pytest.raises(
                    UnsafeDevelopmentPathError, match="硬链接"
                ):
                    await session.execute(
                        text("CREATE TABLE must_not_exist (id INTEGER PRIMARY KEY)")
                    )

                assert protected_database.read_bytes() == protected_before
            finally:
                protected_database.unlink()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='table' AND name='must_not_exist'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_development_database_rechecks_pool_checkout_after_hardlink(
    tmp_path: Path,
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    database_path = app.state.runtime_config.paths.database
    protected_database = tmp_path / "production-pooled.sqlite3"

    async with app.router.lifespan_context(app):
        async with app.state.database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        os.link(database_path, protected_database)
        protected_before = protected_database.read_bytes()
        try:
            with pytest.raises(UnsafeDevelopmentPathError, match="硬链接"):
                async with app.state.database.engine.connect():
                    pass
            assert protected_database.read_bytes() == protected_before
        finally:
            protected_database.unlink()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='table' AND name='must_not_exist'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_development_feedback_rejects_nested_symlink_without_target_writes(
    tmp_path: Path,
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)
    feedback_root = app.state.runtime_config.paths.feedback
    protected_target = tmp_path / "production-feedback"
    protected_target.mkdir()

    async with app.router.lifespan_context(app):
        date_folder = feedback_root / datetime.now(UTC).date().isoformat()
        date_folder.symlink_to(protected_target, target_is_directory=True)
        with pytest.raises(UnsafeDevelopmentPathError):
            await app.state.feedback_writer.write(
                card_id="card-1",
                scene_id="meeting",
                rating="accurate",
                explanation="",
                audio={},
                transcript={},
                qa={},
            )

    assert not any(protected_target.iterdir())


@pytest.mark.asyncio
async def test_development_evidence_audio_stream_survives_directory_swap_without_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    app = create_app(runtime_config=runtime_config)

    async with app.router.lifespan_context(app):
        paths = app.state.paths
        job_id, batch_id, version_id, card_id, file_id = (
            str(uuid4()) for _ in range(5)
        )
        audio_path = paths.audio / "evidence.mp3"
        audio_path.write_bytes(b"development audio")
        async with app.state.database.session() as session:
            session.add(
                AnalysisJob(
                    id=job_id,
                    stage="completed",
                    provider_id="test",
                    model_id="test",
                    prompt_snapshot_json="{}",
                )
            )
            session.add(
                Batch(
                    id=batch_id,
                    job_id=job_id,
                    natural_date="2026-08-19",
                    uploaded_at="2026-08-19T10:00:00+00:00",
                )
            )
            await session.flush()
            session.add(
                AnalysisVersion(
                    id=version_id,
                    source_job_id=job_id,
                    batch_id=batch_id,
                    provider_id="test",
                    model_id="test",
                    credential_generation=0,
                    prompt_snapshot_json="{}",
                    profile_snapshot_json="[]",
                    fixed_rules_hash="rules",
                    staged_results_json=json.dumps(
                        {
                            "meeting": {
                                "cards": [
                                    {"evidence_segment_ids": ["seg_0_0"]}
                                ]
                            }
                        }
                    ),
                    status="completed",
                )
            )
            await session.flush()
            batch = await session.get(Batch, batch_id)
            assert batch is not None
            batch.current_analysis_version_id = version_id
            session.add(
                Card(
                    id=card_id,
                    batch_id=batch_id,
                    analysis_version_id=version_id,
                    scene_id="meeting",
                    position=0,
                    payload_json="{}",
                )
            )
            session.add(
                JobFile(
                    id=file_id,
                    job_id=job_id,
                    original_name="evidence.mp3",
                    extension=".mp3",
                    size_bytes=17,
                    sha256="a" * 64,
                    duration_ms=1_000,
                    position=0,
                    temporary_path=str(audio_path),
                )
            )
            session.add(
                Transcript(
                    id=str(uuid4()),
                    job_file_id=file_id,
                    segment_index=0,
                    start_ms=0,
                    end_ms=1_000,
                    text="evidence",
                    words_json="[]",
                    risk_classified=True,
                    is_reliable=True,
                )
            )
            await session.commit()

        protected_audio = tmp_path / "production-audio"
        protected_audio.mkdir()
        (protected_audio / "evidence.mp3").write_bytes(b"production secret")
        parked_audio = paths.audio.with_name("audio-before-swap")
        original = app.state.content_service.evidence_audio

        async def open_then_swap(card: str, segment: str):
            opened = await original(card, segment)
            paths.audio.rename(parked_audio)
            paths.audio.symlink_to(protected_audio, target_is_directory=True)
            return opened

        monkeypatch.setattr(
            app.state.content_service, "evidence_audio", open_then_swap
        )
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8766"
            ) as client:
                session_response = await client.get(
                    "/api/session",
                    headers={"Origin": "http://127.0.0.1:8766"},
                )
                assert session_response.status_code == 200
                response = await client.get(
                    f"/api/cards/{card_id}/evidence/seg_0_0/audio",
                    headers={
                        "Origin": "http://127.0.0.1:8766",
                        "X-Audio-Memory-Session": session_response.json()["token"],
                        "Range": "bytes=0-10",
                    },
                )
            assert response.status_code == 206
            assert response.content == b"development"
        finally:
            if paths.audio.is_symlink():
                paths.audio.unlink()
                parked_audio.rename(paths.audio)


@pytest.mark.asyncio
async def test_startup_migrates_and_opens_the_local_database(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)
    app = create_app(paths=paths)

    async with app.router.lifespan_context(app):
        assert paths.database.is_file()
        assert app.state.database.path == paths.database

    assert app.state.database.engine.sync_engine.pool.checkedout() == 0
