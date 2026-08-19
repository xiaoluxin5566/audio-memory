from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from audio_memory.config import AppPaths, AppProfile, RuntimeConfig
from audio_memory.main import create_app
from audio_memory.providers.keychain import ERR_SEC_ITEM_NOT_FOUND


class FakeSecurityClient:
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls

    def read(self, service: str, account: str) -> tuple[int, None]:
        self.calls.append((service, account))
        return ERR_SEC_ITEM_NOT_FOUND, None

    def update(self, service: str, account: str, value: bytes) -> int:
        raise AssertionError("isolation acceptance must not update Keychain")

    def add(self, service: str, account: str, value: bytes) -> int:
        raise AssertionError("isolation acceptance must not add Keychain entries")


def writable_paths(paths: AppPaths, *, development: bool) -> set[Path]:
    runtime_names = (
        (
            "audio-memory-dev.pid",
            "audio-memory-dev.log",
            "audio-memory-dev.start.lock",
        )
        if development
        else ("audio-memory.log",)
    )
    return {
        paths.root,
        paths.database,
        paths.runtime,
        paths.lock,
        paths.feedback,
        paths.staging,
        paths.audio,
        paths.prompts,
        paths.local_session,
        *(paths.runtime / name for name in runtime_names),
    }


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def health(app, port: int) -> dict[str, str]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=f"http://127.0.0.1:{port}",
    ) as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    return response.json()


def unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


FIXTURE_SERVER = """
import sys
from pathlib import Path

import uvicorn

import audio_memory.main as main_module
from audio_memory.config import RuntimeConfig


class FakeSecurityClient:
    def read(self, service, account):
        return -25300, None

    def update(self, service, account, value):
        raise AssertionError("fixture must not update Keychain")

    def add(self, service, account, value):
        raise AssertionError("fixture must not add Keychain entries")


profile, home, project_root, bound_port = sys.argv[1:]
main_module.MacSecurityClient = FakeSecurityClient
runtime_config = RuntimeConfig.from_environment(
    home=Path(home),
    project_root=Path(project_root),
    environ={
        "AUDIO_MEMORY_PROFILE": profile,
        "AUDIO_MEMORY_NO_OPEN": "1",
    },
)
app = main_module.create_app(
    runtime_config=runtime_config,
    local_port=int(bound_port),
)
uvicorn.run(
    app,
    host="127.0.0.1",
    port=int(bound_port),
    log_level="error",
    access_log=False,
)
"""


def start_fixture(
    *,
    profile: str,
    home: Path,
    project_root: Path,
    bound_port: int,
) -> tuple[subprocess.Popen[str], dict[str, str]]:
    environment = {
        **os.environ,
        "HOME": str(home),
        "AUDIO_MEMORY_NO_OPEN": "1",
        "AUDIO_MEMORY_PROFILE": "production",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            FIXTURE_SERVER,
            profile,
            str(home),
            str(project_root),
            str(bound_port),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    url = f"http://127.0.0.1:{bound_port}/api/health"
    try:
        with httpx.Client(trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stderr = process.communicate(timeout=1)[1]
                    raise AssertionError(
                        f"{profile} fixture exited during startup: "
                        f"{process.returncode}\n{stderr}"
                    )
                try:
                    response = client.get(url, timeout=0.25)
                except httpx.HTTPError:
                    time.sleep(0.05)
                    continue
                if response.status_code == 200:
                    payload = response.json()
                    assert payload["status"] == "ok"
                    assert payload["profile"] == profile
                    return process, payload
                time.sleep(0.05)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        raise
    stop_fixture(process)
    raise AssertionError(f"{profile} fixture did not become ready")


def stop_fixture(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        _, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        raise AssertionError(f"fixture process {process.pid} did not stop after SIGTERM")
    assert process.returncode in {0, -signal.SIGTERM}, stderr


def database_evidence(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as connection:
        count = int(
            connection.execute("SELECT COUNT(*) FROM isolation_sentinel").fetchone()[0]
        )
    return digest, count


@pytest.mark.asyncio
async def test_development_lifecycle_keeps_production_fixture_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    production = RuntimeConfig.from_environment(
        home=home,
        project_root=project_root,
        environ={"AUDIO_MEMORY_PROFILE": "production"},
    )
    development = RuntimeConfig.from_environment(
        home=home,
        project_root=project_root,
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    security_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "audio_memory.main.MacSecurityClient",
        lambda: FakeSecurityClient(security_calls),
    )

    assert production.paths.database == (
        home / "Library" / "Application Support" / "AudioMemory" / "audio-memory.sqlite3"
    )
    assert production.profile is AppProfile.PRODUCTION
    assert development.profile is AppProfile.DEVELOPMENT
    assert writable_paths(production.paths, development=False).isdisjoint(
        writable_paths(development.paths, development=True)
    )
    assert development.paths.models == production.paths.models
    assert development.paths.models_writable is False

    production_app = create_app(runtime_config=production)
    development_app = create_app(runtime_config=development)

    async with production_app.router.lifespan_context(production_app):
        production_health = await health(production_app, 8765)
    assert production_health["profile"] == "production"

    with sqlite3.connect(production.paths.database) as connection:
        connection.execute(
            "CREATE TABLE isolation_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO isolation_sentinel(value) VALUES ('production-history')"
        )
    production.paths.models.mkdir(parents=True, exist_ok=True)
    model_sentinel = production.paths.models / "shared-model.sentinel"
    model_sentinel.write_text("read-only-model-fixture\n", encoding="utf-8")
    before_database_hash = hashlib.sha256(
        production.paths.database.read_bytes()
    ).hexdigest()
    with sqlite3.connect(production.paths.database) as connection:
        before_row_count = connection.execute(
            "SELECT COUNT(*) FROM isolation_sentinel"
        ).fetchone()[0]
    before_production_tree = tree_snapshot(production.paths.root)

    async with development_app.router.lifespan_context(development_app):
        development_health = await health(development_app, 8766)
        await development_app.state.settings_repository.set_prevent_sleep(True)
        job = await development_app.state.upload_service.create_job()
        development_app.state.local_web_security.issue_session()
        await development_app.state.feedback_writer.write(
            card_id=None,
            scene_id="isolation",
            rating="accurate",
            explanation="temporary fixture",
            audio=[],
            transcript=[],
            qa=[],
        )
        (development.paths.audio / "development-write.sentinel").write_text(
            "temporary development fixture\n", encoding="utf-8"
        )

        assert development_health["profile"] == "development"
        assert (development.paths.staging / job.id).is_dir()
        assert development.paths.local_session.is_file()
        assert all(
            path.resolve().is_relative_to(development.paths.root.resolve())
            for path in development.paths.root.rglob("*")
        )

    after_database_hash = hashlib.sha256(
        production.paths.database.read_bytes()
    ).hexdigest()
    with sqlite3.connect(production.paths.database) as connection:
        after_row_count = connection.execute(
            "SELECT COUNT(*) FROM isolation_sentinel"
        ).fetchone()[0]

    assert after_database_hash == before_database_hash
    assert after_row_count == before_row_count == 1
    assert tree_snapshot(production.paths.root) == before_production_tree
    assert model_sentinel.read_text(encoding="utf-8") == "read-only-model-fixture\n"
    assert {service for service, _ in security_calls} == {
        "Audio Memory",
        "Audio Memory Dev",
    }


def test_sequential_and_simultaneous_fixture_lifecycle_uses_isolated_roots(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    production = RuntimeConfig.from_environment(
        home=home,
        project_root=project_root,
        environ={"AUDIO_MEMORY_PROFILE": "production"},
    )
    development = RuntimeConfig.from_environment(
        home=home,
        project_root=project_root,
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    production_bound_port = unused_loopback_port()
    development_bound_port = unused_loopback_port()
    while development_bound_port == production_bound_port:
        development_bound_port = unused_loopback_port()

    production_process, production_health = start_fixture(
        profile="production",
        home=home,
        project_root=project_root,
        bound_port=production_bound_port,
    )
    sequential_production_pid = production_process.pid
    stop_fixture(production_process)

    with sqlite3.connect(production.paths.database) as connection:
        connection.execute(
            "CREATE TABLE isolation_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO isolation_sentinel(value) VALUES ('production-history')"
        )
    production.paths.models.mkdir(parents=True, exist_ok=True)
    model_sentinel = production.paths.models / "shared-model.sentinel"
    model_sentinel.write_text("read-only-model-fixture\n", encoding="utf-8")
    before_hash, before_count = database_evidence(production.paths.database)
    before_model_hash = hashlib.sha256(model_sentinel.read_bytes()).hexdigest()

    development_process, development_health = start_fixture(
        profile="development",
        home=home,
        project_root=project_root,
        bound_port=development_bound_port,
    )
    sequential_development_pid = development_process.pid
    stop_fixture(development_process)
    after_hash, after_count = database_evidence(production.paths.database)

    simultaneous_production, simultaneous_production_health = start_fixture(
        profile="production",
        home=home,
        project_root=project_root,
        bound_port=production_bound_port,
    )
    try:
        simultaneous_development, simultaneous_development_health = start_fixture(
            profile="development",
            home=home,
            project_root=project_root,
            bound_port=development_bound_port,
        )
        try:
            assert simultaneous_production.pid != simultaneous_development.pid
            with httpx.Client(trust_env=False) as client:
                assert client.get(
                    f"http://127.0.0.1:{production_bound_port}/api/health",
                    timeout=1,
                ).json()["profile"] == "production"
                assert client.get(
                    f"http://127.0.0.1:{development_bound_port}/api/health",
                    timeout=1,
                ).json()["profile"] == "development"
        finally:
            stop_fixture(simultaneous_development)
    finally:
        stop_fixture(simultaneous_production)

    production_files = sorted(
        str(path.relative_to(tmp_path))
        for path in production.paths.root.rglob("*")
        if path.is_file()
    )
    development_files = sorted(
        str(path.relative_to(tmp_path))
        for path in development.paths.root.rglob("*")
        if path.is_file()
    )
    assert production.port == 8765
    assert development.port == 8766
    assert before_hash == after_hash
    assert before_count == after_count == 1
    assert hashlib.sha256(model_sentinel.read_bytes()).hexdigest() == before_model_hash
    assert set(production_files).isdisjoint(development_files)
    assert all(path.startswith("home/") for path in production_files)
    assert all(path.startswith("project/.runtime/dev/") for path in development_files)

    evidence = {
        "logical_ports": {"production": production.port, "development": development.port},
        "bound_loopback_ports": {
            "production": production_bound_port,
            "development": development_bound_port,
        },
        "sequential": {
            "production": {
                "pid": sequential_production_pid,
                "health": production_health,
            },
            "development": {
                "pid": sequential_development_pid,
                "health": development_health,
            },
        },
        "simultaneous": {
            "production": {
                "pid": simultaneous_production.pid,
                "health": simultaneous_production_health,
            },
            "development": {
                "pid": simultaneous_development.pid,
                "health": simultaneous_development_health,
            },
        },
        "production_database": {
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "before_rows": before_count,
            "after_rows": after_count,
        },
        "shared_model": {
            "before_sha256": before_model_hash,
            "after_sha256": hashlib.sha256(model_sentinel.read_bytes()).hexdigest(),
        },
        "temporary_root": str(tmp_path),
        "production_files": production_files,
        "development_files": development_files,
        "fake_keychain": True,
        "provider_calls": 0,
        "pid": os.getpid(),
    }
    print("TASK7_LIFECYCLE_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
