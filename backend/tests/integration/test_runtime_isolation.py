from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

import audio_memory.main as main_module
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


class ObservableFailClosedProviderClient:
    def __init__(
        self,
        events: list[dict[str, object]],
        *args: object,
        **kwargs: object,
    ) -> None:
        self.events = events
        self.events.append({"event": "client_created", "fail_closed": True})

    async def aclose(self) -> None:
        return None

    async def post(self, url: object, *args: object, **kwargs: object) -> None:
        self.events.append(
            {"event": "provider_call", "url": str(url), "blocked": True}
        )
        raise AssertionError("provider network is disabled in isolation acceptance")


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
    result = {
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
    if paths.models_writable:
        result.add(paths.models)
    return result


def assert_resolved_writable_roots_do_not_overlap(
    production: RuntimeConfig,
    development: RuntimeConfig,
) -> None:
    production_root = production.paths.root.resolve()
    development_root = development.paths.root.resolve()
    assert production_root != development_root
    assert production_root not in development_root.parents
    assert development_root not in production_root.parents

    production_writable = {
        path.resolve() for path in writable_paths(production.paths, development=False)
    }
    development_writable = {
        path.resolve() for path in writable_paths(development.paths, development=True)
    }
    assert production_writable.isdisjoint(development_writable)
    assert all(path.is_relative_to(production_root) for path in production_writable)
    assert all(path.is_relative_to(development_root) for path in development_writable)


def complete_tree_snapshot(root: Path) -> dict[str, dict[str, object]]:
    """Snapshot files, directories, and symlinks without following symlinks."""
    assert root.exists()
    paths = [root, *root.rglob("*")]
    snapshot: dict[str, dict[str, object]] = {}
    for path in sorted(paths, key=lambda item: str(item.relative_to(root))):
        relative = "." if path == root else str(path.relative_to(root))
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            snapshot[relative] = {
                "kind": "symlink",
                "mode": mode,
                "mtime_ns": metadata.st_mtime_ns,
                "target": os.readlink(path),
            }
        elif stat.S_ISDIR(metadata.st_mode):
            snapshot[relative] = {
                "kind": "directory",
                "mode": mode,
                "mtime_ns": metadata.st_mtime_ns,
            }
        elif stat.S_ISREG(metadata.st_mode):
            snapshot[relative] = {
                "kind": "file",
                "mode": mode,
                "mtime_ns": metadata.st_mtime_ns,
                "size": metadata.st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        else:
            snapshot[relative] = {
                "kind": "other",
                "mode": mode,
                "mtime_ns": metadata.st_mtime_ns,
            }
    return snapshot


def snapshot_changes(
    before: dict[str, dict[str, object]],
    after: dict[str, dict[str, object]],
) -> set[str]:
    return {
        name
        for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    }


def entries_outside_root(
    entries: set[str],
    *,
    boundary: Path,
    allowed_root: Path,
) -> set[str]:
    allowed_relative = str(allowed_root.resolve().relative_to(boundary.resolve()))
    return {
        entry
        for entry in entries
        if entry != allowed_relative and not entry.startswith(f"{allowed_relative}/")
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
import json
import os
import sys
from pathlib import Path

import uvicorn

import audio_memory.main as main_module
from audio_memory.config import RuntimeConfig


provider_audit = Path(os.environ["AUDIO_MEMORY_TEST_PROVIDER_AUDIT"])
provider_audit.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def record_provider_event(event, **details):
    payload = {"event": event, **details}
    with provider_audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\\n")


class ProviderNetworkBlocked(RuntimeError):
    pass


class FailClosedProviderClient:
    def __init__(self, *args, **kwargs):
        record_provider_event("client_created", fail_closed=True)

    async def aclose(self):
        return None

    async def request(self, method, url, *args, **kwargs):
        record_provider_event(
            "provider_call",
            method=str(method).upper(),
            url=str(url),
            blocked=True,
        )
        raise ProviderNetworkBlocked(
            "provider network is disabled in isolation acceptance"
        )

    async def get(self, url, *args, **kwargs):
        return await self.request("GET", url, *args, **kwargs)

    async def post(self, url, *args, **kwargs):
        return await self.request("POST", url, *args, **kwargs)

    async def put(self, url, *args, **kwargs):
        return await self.request("PUT", url, *args, **kwargs)

    async def delete(self, url, *args, **kwargs):
        return await self.request("DELETE", url, *args, **kwargs)


class FakeSecurityClient:
    def read(self, service, account):
        return -25300, None

    def update(self, service, account, value):
        raise AssertionError("fixture must not update Keychain")

    def add(self, service, account, value):
        raise AssertionError("fixture must not add Keychain entries")


profile, home, project_root, bound_port = sys.argv[1:]
main_module.MacSecurityClient = FakeSecurityClient
main_module.httpx.AsyncClient = FailClosedProviderClient
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
    provider_audit: Path,
) -> tuple[subprocess.Popen[str], dict[str, str]]:
    environment = {
        **os.environ,
        "HOME": str(home),
        "AUDIO_MEMORY_NO_OPEN": "1",
        "AUDIO_MEMORY_PROFILE": "production",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "AUDIO_MEMORY_TEST_PROVIDER_AUDIT": str(provider_audit),
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
        raise AssertionError(
            f"fixture process {process.pid} did not stop after SIGTERM"
        )
    assert process.returncode in {0, -signal.SIGTERM}, stderr


def database_evidence(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as connection:
        count = int(
            connection.execute("SELECT COUNT(*) FROM isolation_sentinel").fetchone()[0]
        )
    return digest, count


def read_provider_events(path: Path) -> list[dict[str, object]]:
    assert path.is_file()
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_development_lifecycle_keeps_complete_temp_boundary_unchanged(
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
    development.paths.root.mkdir(mode=0o700, parents=True)
    controls = tmp_path / "test-controls"
    controls.mkdir(mode=0o700)
    boundary_target = controls / "symlink-target"
    boundary_target.write_text("boundary sentinel\n", encoding="utf-8")
    boundary_symlink = controls / "boundary-symlink"
    boundary_symlink.symlink_to(boundary_target.name)
    security_calls: list[tuple[str, str]] = []
    provider_events: list[dict[str, object]] = []
    monkeypatch.setattr(
        "audio_memory.main.MacSecurityClient",
        lambda: FakeSecurityClient(security_calls),
    )
    monkeypatch.setattr(
        main_module,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda *args, **kwargs: ObservableFailClosedProviderClient(
                provider_events,
                *args,
                **kwargs,
            )
        ),
    )

    assert production.paths.database == (
        home
        / "Library"
        / "Application Support"
        / "AudioMemory"
        / "audio-memory.sqlite3"
    )
    assert production.profile is AppProfile.PRODUCTION
    assert development.profile is AppProfile.DEVELOPMENT
    assert_resolved_writable_roots_do_not_overlap(production, development)
    assert development.paths.models == production.paths.models
    assert development.paths.models_writable is False

    production_app = create_app(runtime_config=production)
    development_app = create_app(runtime_config=development)

    async with production_app.router.lifespan_context(production_app):
        production_health = await health(production_app, 8765)
    assert production_health["profile"] == "production"

    with sqlite3.connect(production.paths.database) as connection:
        connection.execute(
            "CREATE TABLE isolation_sentinel "
            "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO isolation_sentinel(value) VALUES ('production-history')"
        )
    production.paths.models.mkdir(parents=True, exist_ok=True)
    model_sentinel = production.paths.models / "shared-model.sentinel"
    model_sentinel.write_text("read-only-model-fixture\n", encoding="utf-8")
    before_database_hash, before_row_count = database_evidence(
        production.paths.database
    )
    before_production_tree = complete_tree_snapshot(production.paths.root)
    before_boundary = complete_tree_snapshot(tmp_path)
    boundary_symlink_entry = before_boundary["test-controls/boundary-symlink"]
    assert boundary_symlink_entry["kind"] == "symlink"
    assert boundary_symlink_entry["target"] == "symlink-target"
    assert isinstance(boundary_symlink_entry["mode"], int)

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
            "temporary development fixture\n",
            encoding="utf-8",
        )

        assert development_health["profile"] == "development"
        assert (development.paths.staging / job.id).is_dir()
        assert development.paths.local_session.is_file()

    after_database_hash, after_row_count = database_evidence(production.paths.database)
    after_boundary = complete_tree_snapshot(tmp_path)
    all_changes = snapshot_changes(before_boundary, after_boundary)
    assert entries_outside_root(
        all_changes,
        boundary=tmp_path,
        allowed_root=development.paths.root,
    ) == set()

    development_tree = complete_tree_snapshot(development.paths.root)
    assert development_tree["runtime/audio-memory.lock"]["kind"] == "file"
    assert development_tree["runtime/local-web-security.sqlite3"]["kind"] == "file"
    assert development_tree["audio-memory.sqlite3"]["kind"] == "file"
    assert development_tree["audio/development-write.sentinel"]["kind"] == "file"
    assert {
        Path(name).parts[0]
        for name in development_tree
        if name != "."
    } <= {
        "audio-memory.sqlite3",
        "audio",
        "prompts",
        "runtime",
        "staging",
        "意见反馈",
    }
    assert all(entry["kind"] != "symlink" for entry in development_tree.values())
    assert after_database_hash == before_database_hash
    assert after_row_count == before_row_count == 1
    assert complete_tree_snapshot(production.paths.root) == before_production_tree
    assert model_sentinel.read_text(encoding="utf-8") == "read-only-model-fixture\n"
    assert {service for service, _ in security_calls} == {
        "Audio Memory",
        "Audio Memory Dev",
    }
    assert sum(event["event"] == "client_created" for event in provider_events) == 14
    provider_calls = sum(
        event["event"] == "provider_call" for event in provider_events
    )
    assert 0 <= provider_calls <= 2
    assert all(
        event["url"].endswith("/v1/installations")
        for event in provider_events
        if event["event"] == "provider_call"
    )


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
    assert_resolved_writable_roots_do_not_overlap(production, development)
    development.paths.root.mkdir(mode=0o700, parents=True)
    controls = tmp_path / "test-controls"
    controls.mkdir(mode=0o700)
    provider_audits = {
        name: controls / f"{name}-provider-events.jsonl"
        for name in (
            "sequential-production",
            "sequential-development",
            "simultaneous-production",
            "simultaneous-development",
        )
    }
    for audit in provider_audits.values():
        audit.touch(mode=0o600)

    production_bound_port = unused_loopback_port()
    development_bound_port = unused_loopback_port()
    while development_bound_port == production_bound_port:
        development_bound_port = unused_loopback_port()

    production_process, production_health = start_fixture(
        profile="production",
        home=home,
        project_root=project_root,
        bound_port=production_bound_port,
        provider_audit=provider_audits["sequential-production"],
    )
    sequential_production_pid = production_process.pid
    stop_fixture(production_process)

    with sqlite3.connect(production.paths.database) as connection:
        connection.execute(
            "CREATE TABLE isolation_sentinel "
            "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO isolation_sentinel(value) VALUES ('production-history')"
        )
    production.paths.models.mkdir(parents=True, exist_ok=True)
    model_sentinel = production.paths.models / "shared-model.sentinel"
    model_sentinel.write_text("read-only-model-fixture\n", encoding="utf-8")
    before_hash, before_count = database_evidence(production.paths.database)
    before_model_hash = hashlib.sha256(model_sentinel.read_bytes()).hexdigest()
    before_production_tree = complete_tree_snapshot(production.paths.root)
    before_development = complete_tree_snapshot(tmp_path)

    development_process, development_health = start_fixture(
        profile="development",
        home=home,
        project_root=project_root,
        bound_port=development_bound_port,
        provider_audit=provider_audits["sequential-development"],
    )
    sequential_development_pid = development_process.pid
    stop_fixture(development_process)
    after_hash, after_count = database_evidence(production.paths.database)
    after_development = complete_tree_snapshot(tmp_path)

    changed_outside_development = entries_outside_root(
        snapshot_changes(before_development, after_development),
        boundary=tmp_path,
        allowed_root=development.paths.root,
    )
    expected_control_change = str(
        provider_audits["sequential-development"].relative_to(tmp_path)
    )
    assert changed_outside_development == {expected_control_change}
    assert complete_tree_snapshot(production.paths.root) == before_production_tree

    simultaneous_production, simultaneous_production_health = start_fixture(
        profile="production",
        home=home,
        project_root=project_root,
        bound_port=production_bound_port,
        provider_audit=provider_audits["simultaneous-production"],
    )
    try:
        simultaneous_development, simultaneous_development_health = start_fixture(
            profile="development",
            home=home,
            project_root=project_root,
            bound_port=development_bound_port,
            provider_audit=provider_audits["simultaneous-development"],
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

    provider_events = {
        name: read_provider_events(path) for name, path in provider_audits.items()
    }
    provider_clients_created = {
        name: sum(event["event"] == "client_created" for event in events)
        for name, events in provider_events.items()
    }
    provider_calls = sum(
        event["event"] == "provider_call"
        for events in provider_events.values()
        for event in events
    )
    assert all(count == 7 for count in provider_clients_created.values())
    assert all(
        event.get("fail_closed") is True
        for events in provider_events.values()
        for event in events
        if event["event"] == "client_created"
    )
    assert 0 <= provider_calls <= 4
    assert all(
        str(event["url"]).endswith("/v1/installations")
        for events in provider_events.values()
        for event in events
        if event["event"] == "provider_call"
    )

    production_snapshot = complete_tree_snapshot(production.paths.root)
    development_snapshot = complete_tree_snapshot(development.paths.root)
    production_files = sorted(
        str(production.paths.root.relative_to(tmp_path) / name)
        for name, metadata in production_snapshot.items()
        if name != "." and metadata["kind"] == "file"
    )
    development_files = sorted(
        str(development.paths.root.relative_to(tmp_path) / name)
        for name, metadata in development_snapshot.items()
        if name != "." and metadata["kind"] == "file"
    )
    assert production.port == 8765
    assert development.port == 8766
    assert before_hash == after_hash
    assert before_count == after_count == 1
    assert hashlib.sha256(model_sentinel.read_bytes()).hexdigest() == before_model_hash
    assert set(production_files).isdisjoint(development_files)
    assert all(path.startswith("home/") for path in production_files)
    assert all(path.startswith("project/.runtime/dev/") for path in development_files)
    assert production_snapshot["runtime/audio-memory.lock"]["kind"] == "file"
    assert development_snapshot["runtime/audio-memory.lock"]["kind"] == "file"

    evidence = {
        "logical_ports": {
            "production": production.port,
            "development": development.port,
        },
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
        "boundary_snapshot": {
            "entry_kinds": ["directory", "file", "symlink"],
            "metadata": [
                "mode",
                "mtime_ns",
                "size",
                "sha256",
                "symlink_target",
            ],
            "changes_outside_development": sorted(changed_outside_development),
            "accounted_test_control": expected_control_change,
            "production_tree_unchanged_after_development": True,
        },
        "temporary_root": str(tmp_path),
        "production_files": production_files,
        "development_files": development_files,
        "fake_keychain": True,
        "provider_boundary": {
            "fail_closed": True,
            "clients_created": provider_clients_created,
        },
        "provider_calls": provider_calls,
        "pid": os.getpid(),
    }
    print("TASK7_LIFECYCLE_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
