from __future__ import annotations

import fcntl
import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_HELPER = REPOSITORY_ROOT / "scripts" / "runtime_config.py"
DEV_START = REPOSITORY_ROOT / "scripts" / "dev-start.sh"
DEV_STOP = REPOSITORY_ROOT / "scripts" / "dev-stop.sh"
PYTHON = REPOSITORY_ROOT / "backend" / ".venv" / "bin" / "python"
REAL_PYTHON = str(PYTHON)
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
import dev_lifecycle  # noqa: E402


def isolated_environment(home: Path, **overrides: str) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(REPOSITORY_ROOT / "backend" / "src"),
    }
    environment.update(overrides)
    return environment


def copied_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    backend = project_root / "backend"
    backend.mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / "backend" / "pyproject.toml", backend)
    return project_root


def run_helper(
    *, project_root: Path, home: Path, overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(PYTHON),
            str(RUNTIME_HELPER),
            "development-env",
            "--project-root",
            str(project_root),
            "--home",
            str(home),
        ],
        cwd=REPOSITORY_ROOT,
        env=isolated_environment(home, **(overrides or {})),
        capture_output=True,
        text=True,
        check=False,
    )


def parse_assignments(output: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line in output.splitlines():
        tokens = shlex.split(line)
        assert len(tokens) == 1, line
        name, value = tokens[0].split("=", 1)
        assert name not in assignments
        assignments[name] = value
    return assignments


def assert_no_runtime_artifacts(data_root: Path) -> None:
    forbidden = (
        data_root / "audio-memory.sqlite3",
        data_root / "runtime" / "audio-memory.lock",
        data_root / "runtime" / "audio-memory-dev.pid",
        data_root / "runtime" / "audio-memory-dev.log",
    )
    assert all(not path.exists() for path in forbidden)


def write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def expected_server_argv(port: int = 8766) -> list[str]:
    backend_source = REPOSITORY_ROOT / "backend" / "src"
    return [
        REAL_PYTHON,
        "-m",
        "uvicorn",
        "audio_memory.main:app",
        "--app-dir",
        str(backend_source),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def test_server_argv_preserves_virtualenv_python_path() -> None:
    argv = dev_lifecycle._server_argv(REPOSITORY_ROOT, 8766)

    assert argv[0] == str(PYTHON)


def test_server_argv_can_use_a_verified_shared_toolchain_python(
    tmp_path: Path,
) -> None:
    shared_python = tmp_path / "toolchain" / "python"
    shared_python.parent.mkdir()
    shared_python.write_bytes(b"python")
    shared_python.chmod(0o755)

    argv = dev_lifecycle._server_argv(
        REPOSITORY_ROOT, 8766, python_executable=shared_python
    )

    assert argv[0] == str(shared_python.resolve())
    assert argv[5] == str(REPOSITORY_ROOT / "backend" / "src")


def test_server_argv_rejects_untrusted_shared_python(tmp_path: Path) -> None:
    shared_python = tmp_path / "python"
    shared_python.write_bytes(b"not executable")

    with pytest.raises(dev_lifecycle.LifecycleError, match="Python"):
        dev_lifecycle._server_argv(
            REPOSITORY_ROOT, 8766, python_executable=shared_python
        )


def process_record(
    *,
    pid: int = 4242,
    started_at: str = "Tue Aug 19 11:00:00 2026",
    argv: list[str] | None = None,
    command: str | None = None,
    phase: str = "ready",
) -> dict[str, object]:
    resolved_argv = argv or expected_server_argv()
    return {
        "version": 1,
        "pid": pid,
        "started_at": started_at,
        "argv": resolved_argv,
        "command": command or " ".join(resolved_argv),
        "port": 8766,
        "phase": phase,
    }


def write_process_record(data_root: Path, record: dict[str, object]) -> Path:
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    pid_file = runtime / "audio-memory-dev.pid"
    pid_file.write_text(json.dumps(record), encoding="utf-8")
    return pid_file


def lifecycle_fakes(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    write_executable(
        fake_bin / "ps",
        'printf "ps %s\\n" "$*" >> "$FAKE_CALLS"\n'
        '[ "${FAKE_PS_STATUS:-0}" = "0" ] || exit "$FAKE_PS_STATUS"\n'
        'case "$*" in\n'
        '  *lstart=*)\n'
        '    count_file="$FAKE_PS_START_COUNT"\n'
        '    count=0; [ ! -f "$count_file" ] || count="$(sed -n 1p "$count_file")"\n'
        '    count=$((count + 1)); printf "%s\\n" "$count" > "$count_file"\n'
        '    if [ "$count" -gt 1 ] && [ -n "${FAKE_PS_START_SECOND:-}" ]; then\n'
        '      printf "%s\\n" "$FAKE_PS_START_SECOND"\n'
        '    else\n'
        '      printf "%s\\n" "$FAKE_PS_START"\n'
        '    fi ;;\n'
        '  *) printf "%s\\n" "$FAKE_PS_COMMAND" ;;\n'
        'esac',
    )
    write_executable(
        fake_bin / "curl",
        'printf "curl %s\\n" "$*" >> "$FAKE_CALLS"\n'
        '[ "${FAKE_CURL_STATUS:-0}" = "0" ] || exit "$FAKE_CURL_STATUS"\n'
        'printf "%s\\n" "${FAKE_HEALTH:-}"',
    )
    write_executable(
        fake_bin / "lsof",
        'printf "lsof %s\\n" "$*" >> "$FAKE_CALLS"\n'
        '[ "${FAKE_LSOF_STATUS:-0}" = "0" ] || exit "$FAKE_LSOF_STATUS"\n'
        'printf "%s\\n" "${FAKE_LSOF_OUTPUT:-}"',
    )
    write_executable(
        fake_bin / "kill", 'printf "kill %s\\n" "$*" >> "$FAKE_CALLS"'
    )
    write_executable(
        fake_bin / "env",
        'printf "spawned\\n" >> "$FAKE_CALLS"\nprintf "redirected\\n"',
    )
    write_executable(
        fake_bin / "launchctl",
        'printf "launchctl %s\\n" "$*" >> "$FAKE_CALLS"; exit 99',
    )
    return fake_bin, calls


def run_start(
    *,
    home: Path,
    data_root: Path,
    fake_bin: Path,
    calls: Path,
    health: str = "",
    curl_status: int = 1,
    lsof_status: int = 1,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(DEV_START)],
        cwd=REPOSITORY_ROOT,
        env=isolated_environment(
            home,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_CALLS=str(calls),
            FAKE_PS_START_COUNT=str(home.parent / "ps-start-count"),
            FAKE_PS_START="Tue Aug 19 11:00:00 2026",
            FAKE_PS_COMMAND=" ".join(expected_server_argv()),
            FAKE_HEALTH=health,
            FAKE_CURL_STATUS=str(curl_status),
            FAKE_LSOF_STATUS=str(lsof_status),
            AUDIO_MEMORY_DATA_ROOT=str(data_root),
            AUDIO_MEMORY_NO_OPEN="1",
        ),
        capture_output=True,
        text=True,
        check=False,
    )


def run_stop(
    *,
    home: Path,
    data_root: Path,
    fake_bin: Path,
    calls: Path,
    ps_command: str | None = None,
    ps_status: int = 0,
    start_identity: str = "Tue Aug 19 11:00:00 2026",
    second_start_identity: str = "",
    listener_pid: str = "4242",
    lsof_status: int = 0,
    health: str = '{"status":"ok","profile":"development"}',
    curl_status: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(DEV_STOP)],
        cwd=REPOSITORY_ROOT,
        env=isolated_environment(
            home,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_CALLS=str(calls),
            FAKE_PS_STATUS=str(ps_status),
            FAKE_PS_COMMAND=ps_command or " ".join(expected_server_argv()),
            FAKE_PS_START=start_identity,
            FAKE_PS_START_SECOND=second_start_identity,
            FAKE_PS_START_COUNT=str(home.parent / "ps-start-count"),
            FAKE_LSOF_STATUS=str(lsof_status),
            FAKE_LSOF_OUTPUT=listener_pid,
            FAKE_HEALTH=health,
            FAKE_CURL_STATUS=str(curl_status),
            AUDIO_MEMORY_DATA_ROOT=str(data_root),
        ),
        capture_output=True,
        text=True,
        check=False,
    )


def recorded_calls(calls: Path) -> list[str]:
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


class FakeServerProcess:
    def __init__(self, argv: list[str], **kwargs: object) -> None:
        self.pid = 4242
        self.argv = argv
        self.environment = dict(kwargs["env"])  # type: ignore[arg-type]
        self.returncode: int | None = None
        self.signals: list[int] = []
        self._finished = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        self.complete(-signum)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def complete(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self._finished.set()


def wait_for_record_phase(
    path: Path, phase: str, timeout: float = 2.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        if record.get("phase") == phase:
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path} phase {phase}")


def launch_fake_successful_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[
    threading.Thread,
    list[int],
    FakeServerProcess,
    Path,
    dict[int, object],
]:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = data_root / "runtime" / "audio-memory-dev.pid"
    results: list[int] = []
    handlers: dict[int, object] = {}
    health_calls = 0
    fake_process: FakeServerProcess | None = None

    monkeypatch.setenv("AUDIO_MEMORY_DATA_ROOT", str(data_root))
    monkeypatch.delenv("AUDIO_MEMORY_MODEL_ROOT", raising=False)
    monkeypatch.setattr(dev_lifecycle, "_port_is_occupied", lambda port: False)

    def fake_health(port: int) -> tuple[str, dict[str, str] | None]:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            return "unavailable", None
        return "ok", {"status": "ok", "profile": "development"}

    def fake_popen(argv: list[str], **kwargs: object) -> FakeServerProcess:
        nonlocal fake_process
        fake_process = FakeServerProcess(argv, **kwargs)
        return fake_process

    monkeypatch.setattr(dev_lifecycle, "_health", fake_health)
    monkeypatch.setattr(dev_lifecycle.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        dev_lifecycle,
        "_snapshot",
        lambda pid: (
            " ".join(expected_server_argv()),
            "Tue Aug 19 11:00:00 2026",
        ),
    )
    class FakeSignalModule:
        SIGINT = signal.SIGINT
        SIGTERM = signal.SIGTERM
        SIG_DFL = signal.SIG_DFL

        @staticmethod
        def signal(signum: int, handler: object) -> object:
            previous = handlers.get(signum, signal.SIG_DFL)
            handlers[signum] = handler
            return previous

    monkeypatch.setattr(dev_lifecycle, "signal", FakeSignalModule, raising=False)

    thread = threading.Thread(
        target=lambda: results.append(dev_lifecycle.start(REPOSITORY_ROOT, home)),
        daemon=True,
    )
    thread.start()
    wait_for_record_phase(pid_file, "ready")
    assert fake_process is not None
    return thread, results, fake_process, pid_file, handlers


def test_development_env_resolves_validated_defaults_without_writes(
    tmp_path: Path,
) -> None:
    project_root = copied_project(tmp_path)
    home = tmp_path / "home"
    result = run_helper(project_root=project_root, home=home)

    assert result.returncode == 0, result.stderr
    assignments = parse_assignments(result.stdout)
    production_root = home / "Library" / "Application Support" / "AudioMemory"
    assert assignments["AUDIO_MEMORY_PROFILE"] == "development"
    assert assignments["AUDIO_MEMORY_DATA_ROOT"] == str(
        (project_root / ".runtime" / "dev").resolve()
    )
    assert "AUDIO_MEMORY_MODEL_ROOT" not in assignments
    assert "AUDIO_MEMORY_MODELS_WRITABLE" not in assignments
    assert assignments["AUDIO_MEMORY_KEYCHAIN_SERVICE"] == "Audio Memory Dev"
    assert assignments["AUDIO_MEMORY_PORT"] == "8766"
    assert assignments["AUDIO_MEMORY_RUNTIME_DIR"] == str(
        (project_root / ".runtime" / "dev" / "runtime").resolve()
    )
    assert not (project_root / ".runtime").exists()
    assert not production_root.exists()

    roundtrip = run_helper(
        project_root=project_root,
        home=home,
        overrides=assignments,
    )
    assert roundtrip.returncode == 0, roundtrip.stderr
    assert parse_assignments(roundtrip.stdout) == assignments
    assert not (project_root / ".runtime").exists()
    assert not production_root.exists()


def test_open_runtime_rejects_data_root_symlink_swap_without_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    data_root = project_root / ".runtime/dev"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    protected_target = tmp_path / "production-target"
    protected_runtime = protected_target / "runtime"
    protected_runtime.mkdir(parents=True, mode=0o755)
    protected_mode = protected_runtime.stat().st_mode & 0o777
    config = dev_lifecycle.development_config(
        project_root=project_root, home=home
    )
    parked_root = data_root.with_name("dev-before-swap")
    config_type = type(config)
    original_validate = config_type.validate_development_isolation
    swapped = False

    def validate_then_swap(candidate: object) -> None:
        nonlocal swapped
        original_validate(candidate)  # type: ignore[arg-type]
        if candidate is config and not swapped:
            data_root.rename(parked_root)
            data_root.symlink_to(protected_target, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(
        config_type, "validate_development_isolation", validate_then_swap
    )

    with pytest.raises(dev_lifecycle.LifecycleError):
        runtime_fd = dev_lifecycle._open_runtime(config, create=True)
        if runtime_fd is not None:
            os.close(runtime_fd)

    assert swapped is True
    assert protected_runtime.stat().st_mode & 0o777 == protected_mode
    assert not any(protected_runtime.iterdir())


def test_open_runtime_validates_before_creating_any_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    config = dev_lifecycle.development_config(
        project_root=project_root, home=home
    )
    config_type = type(config)

    def reject_before_write(candidate: object) -> None:
        assert candidate is config
        raise dev_lifecycle.RuntimeConfigurationError("injected rejection")

    monkeypatch.setattr(
        config_type, "validate_development_isolation", reject_before_write
    )

    with pytest.raises(dev_lifecycle.LifecycleError):
        dev_lifecycle._open_runtime(config, create=True)

    assert not config.paths.root.exists()


@pytest.mark.parametrize(
    ("name", "flags"),
    [
        (dev_lifecycle.LOG_NAME, os.O_WRONLY | os.O_APPEND),
        (dev_lifecycle.PID_NAME, os.O_RDONLY),
    ],
)
def test_runtime_open_rejects_log_and_pid_hardlinks_after_root_is_pinned(
    tmp_path: Path,
    name: str,
    flags: int,
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    config = dev_lifecycle.development_config(
        project_root=project_root, home=home
    )
    runtime_fd = dev_lifecycle._open_runtime(config, create=True)
    assert runtime_fd is not None
    protected = tmp_path / f"production-{name}"
    protected.write_bytes(b"preserve exactly")
    runtime_path = config.paths.runtime / name
    os.link(protected, runtime_path)

    try:
        with pytest.raises(dev_lifecycle.LifecycleError, match="硬链接"):
            fd = dev_lifecycle._open_regular_at(runtime_fd, name, flags)
            try:
                if flags & os.O_WRONLY:
                    os.write(fd, b"mutated")
            finally:
                os.close(fd)
    finally:
        os.close(runtime_fd)

    assert protected.read_bytes() == b"preserve exactly"


def test_development_env_preserves_explicit_dev_local_model_root_roundtrip(
    tmp_path: Path,
) -> None:
    project_root = copied_project(tmp_path)
    home = tmp_path / "home"
    data_root = project_root / ".runtime/dev"
    model_root = data_root / "models"
    result = run_helper(
        project_root=project_root,
        home=home,
        overrides={
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_MODEL_ROOT": str(model_root),
        },
    )

    assert result.returncode == 0, result.stderr
    assignments = parse_assignments(result.stdout)
    assert assignments["AUDIO_MEMORY_MODEL_ROOT"] == str(model_root.resolve())
    roundtrip = run_helper(
        project_root=project_root, home=home, overrides=assignments
    )
    assert roundtrip.returncode == 0, roundtrip.stderr
    assert parse_assignments(roundtrip.stdout) == assignments
    assert not data_root.exists()


@pytest.mark.parametrize("relative_data_root", [".", "child"])
def test_development_env_rejects_production_root_and_child_before_writes(
    tmp_path: Path, relative_data_root: str
) -> None:
    project_root = copied_project(tmp_path)
    home = tmp_path / "home"
    production_root = home / "Library" / "Application Support" / "AudioMemory"
    requested_root = (production_root / relative_data_root).resolve()
    result = run_helper(
        project_root=project_root,
        home=home,
        overrides={"AUDIO_MEMORY_DATA_ROOT": str(requested_root)},
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "开发数据目录不能与正式数据目录重叠" in result.stderr
    assert_no_runtime_artifacts(requested_root)


def test_development_env_rejects_symlink_escape_before_writes(tmp_path: Path) -> None:
    project_root = copied_project(tmp_path)
    home = tmp_path / "home"
    production_root = home / "Library" / "Application Support" / "AudioMemory"
    production_root.mkdir(parents=True)
    link = tmp_path / "development-link"
    link.symlink_to(production_root, target_is_directory=True)
    requested_root = link / "escaped"
    result = run_helper(
        project_root=project_root,
        home=home,
        overrides={"AUDIO_MEMORY_DATA_ROOT": str(requested_root)},
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "开发数据目录不能与正式数据目录重叠" in result.stderr
    assert_no_runtime_artifacts(requested_root)


def test_development_env_rejects_nested_runtime_symlink(tmp_path: Path) -> None:
    project_root = copied_project(tmp_path)
    home = tmp_path / "home"
    data_root = project_root / ".runtime/dev"
    outside = tmp_path / "outside"
    data_root.mkdir(parents=True)
    outside.mkdir()
    (data_root / "runtime").symlink_to(outside, target_is_directory=True)

    result = run_helper(project_root=project_root, home=home)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "派生可写路径" in result.stderr
    assert not any(outside.iterdir())


def test_dev_start_refuses_an_occupied_port_without_runtime_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    fake_bin, calls = lifecycle_fakes(tmp_path)
    write_executable(
        fake_bin / "lsof",
        'printf "lsof %s\\n" "$*" >> "$FAKE_CALLS"\nexit 0',
    )
    result = run_start(
        home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
    )

    assert result.returncode != 0
    assert "端口 8766 已被其他程序占用" in result.stderr
    assert_no_runtime_artifacts(data_root)
    assert all("launchctl" not in call for call in recorded_calls(calls))


def test_dev_start_rejects_development_profile_with_error_health_status(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_start(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        health='{"status":"error","profile":"development"}',
        curl_status=0,
    )

    assert result.returncode != 0
    assert "不是可用的 Audio Memory 开发环境" in result.stderr
    assert_no_runtime_artifacts(data_root)


def test_dev_start_refuses_log_symlink_without_following_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    target = tmp_path / "outside-log"
    target.write_text("keep\n", encoding="utf-8")
    (runtime / "audio-memory-dev.log").symlink_to(target)
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_start(
        home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
    )

    assert result.returncode != 0
    assert "符号链接" in result.stderr
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert not (runtime / "audio-memory-dev.pid").exists()
    assert "spawned" not in recorded_calls(calls)


def test_dev_start_refuses_when_another_start_holds_the_guard(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    guard = runtime / "audio-memory-dev.start.lock"
    guard_fd = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fake_bin, calls = lifecycle_fakes(tmp_path)
    try:
        result = run_start(
            home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
        )
    finally:
        os.close(guard_fd)

    assert result.returncode != 0
    assert "另一个开发启动正在进行" in result.stderr
    assert "spawned" not in recorded_calls(calls)
    assert not (runtime / "audio-memory-dev.pid").exists()


def test_dev_start_success_publishes_ready_identity_and_holds_guard_until_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, results, child, pid_file, _ = launch_fake_successful_start(
        monkeypatch, tmp_path
    )
    record = json.loads(pid_file.read_text(encoding="utf-8"))
    guard = pid_file.parent / "audio-memory-dev.start.lock"
    competing_guard_fd = os.open(guard, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(
                competing_guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
            )
    finally:
        os.close(competing_guard_fd)

    assert record["pid"] == child.pid
    assert record["phase"] == "ready"
    assert record["argv"] == expected_server_argv()
    assert "AUDIO_MEMORY_MODEL_ROOT" not in child.environment
    assert thread.is_alive()

    child.complete(0)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert results == [0]
    assert not pid_file.exists()
    released_guard_fd = os.open(guard, os.O_RDWR)
    try:
        fcntl.flock(released_guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(released_guard_fd)


def test_dev_start_sigterm_forwards_and_cleans_own_record_and_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, results, child, pid_file, handlers = launch_fake_successful_start(
        monkeypatch, tmp_path
    )
    own_record = pid_file.read_bytes()
    replacement = json.dumps(process_record(pid=9999)).encode("utf-8")
    pid_file.write_bytes(replacement)
    guard = pid_file.parent / "audio-memory-dev.start.lock"
    handler = handlers[signal.SIGTERM]

    assert callable(handler)
    handler(signal.SIGTERM, None)
    thread.join(timeout=2)

    assert child.signals == [signal.SIGTERM]
    assert not thread.is_alive()
    assert results == [-signal.SIGTERM]
    assert own_record
    assert pid_file.read_bytes() == replacement
    released_guard_fd = os.open(guard, os.O_RDWR)
    try:
        fcntl.flock(released_guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(released_guard_fd)


def test_dev_stop_rejects_pid_symlink_without_following_target(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    target = tmp_path / "outside-pid"
    target.write_text("4242\n", encoding="utf-8")
    pid_file = runtime / "audio-memory-dev.pid"
    pid_file.symlink_to(target)
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
    )

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "4242\n"
    assert pid_file.is_symlink()
    assert "符号链接" in result.stderr
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


@pytest.mark.parametrize(
    ("ps_status", "ps_command"),
    [(1, None), (0, "/usr/bin/python unrelated_service.py --port 8766")],
)
def test_dev_stop_removes_dead_or_command_mismatched_record_without_term(
    tmp_path: Path, ps_status: int, ps_command: str | None
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = write_process_record(data_root, process_record())
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        ps_status=ps_status,
        ps_command=ps_command,
    )

    assert result.returncode != 0
    assert not pid_file.exists()
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_preserves_valid_record_when_health_is_unavailable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = write_process_record(data_root, process_record())
    original = pid_file.read_bytes()
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        curl_status=1,
    )

    assert result.returncode != 0
    assert "健康检查暂时不可用" in result.stderr
    assert pid_file.read_bytes() == original
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_preserves_valid_record_for_error_health_status(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = write_process_record(data_root, process_record())
    original = pid_file.read_bytes()
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        health='{"status":"error","profile":"development"}',
    )

    assert result.returncode != 0
    assert "开发服务健康状态不是 ok" in result.stderr
    assert pid_file.read_bytes() == original
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_preserves_starting_record_when_guard_is_held(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = write_process_record(
        data_root, process_record(phase="starting")
    )
    original = pid_file.read_bytes()
    guard = pid_file.parent / "audio-memory-dev.start.lock"
    guard_fd = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fake_bin, calls = lifecycle_fakes(tmp_path)
    try:
        result = run_stop(
            home=home,
            data_root=data_root,
            fake_bin=fake_bin,
            calls=calls,
            listener_pid="",
            lsof_status=1,
        )
    finally:
        os.close(guard_fd)

    assert result.returncode != 0
    assert "开发启动尚未就绪" in result.stderr
    assert pid_file.read_bytes() == original
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_requires_pid_to_own_expected_listener(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = write_process_record(data_root, process_record())
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        listener_pid="9999",
    )

    assert result.returncode != 0
    assert not pid_file.exists()
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_revalidates_start_identity_after_health_before_term(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = write_process_record(data_root, process_record())
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        second_start_identity="Tue Aug 19 11:01:00 2026",
    )

    assert result.returncode != 0
    assert not pid_file.exists()
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_rejects_non_exact_recorded_argv(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    argv = expected_server_argv() + ["--reload"]
    pid_file = write_process_record(data_root, process_record(argv=argv))
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
    )

    assert result.returncode != 0
    assert not pid_file.exists()
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_removes_malformed_non_string_phase(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    record = process_record()
    record["phase"] = []
    pid_file = write_process_record(data_root, record)
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
    )

    assert result.returncode != 0
    assert "PID 记录身份无效" in result.stderr
    assert not pid_file.exists()
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_rejects_recorded_command_that_is_not_exact_expected(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    spoofed_command = " ".join(expected_server_argv()) + " --reload"
    pid_file = write_process_record(
        data_root, process_record(command=spoofed_command)
    )
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        ps_command=spoofed_command,
    )

    assert result.returncode != 0
    assert not pid_file.exists()
    assert not any(call.startswith("kill ") for call in recorded_calls(calls))


def test_dev_stop_sends_term_only_after_two_complete_identity_checks(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    pid_file = write_process_record(data_root, process_record())
    fake_bin, calls = lifecycle_fakes(tmp_path)
    result = run_stop(
        home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
    )

    assert result.returncode == 0, result.stderr
    assert not pid_file.exists()
    calls_log = recorded_calls(calls)
    assert [call for call in calls_log if call.startswith("kill ")] == [
        "kill -TERM -- 4242"
    ]
    assert len([call for call in calls_log if "lstart=" in call]) == 2
    assert len([call for call in calls_log if call.startswith("lsof ")]) == 2
    assert all("launchctl" not in call for call in calls_log)
