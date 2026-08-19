from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_HELPER = REPOSITORY_ROOT / "scripts" / "runtime_config.py"
DEV_START = REPOSITORY_ROOT / "scripts" / "dev-start.sh"
DEV_STOP = REPOSITORY_ROOT / "scripts" / "dev-stop.sh"
PYTHON = REPOSITORY_ROOT / "backend" / ".venv" / "bin" / "python"


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


def lifecycle_fakes(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    write_executable(
        fake_bin / "ps",
        '[ "${FAKE_PS_STATUS:-0}" = "0" ] || exit "$FAKE_PS_STATUS"\n'
        'printf "%s\\n" "${FAKE_PS_COMMAND:-}"',
    )
    write_executable(
        fake_bin / "curl",
        '[ "${FAKE_CURL_STATUS:-0}" = "0" ] || exit "$FAKE_CURL_STATUS"\n'
        'printf "%s\\n" "${FAKE_HEALTH:-}"',
    )
    write_executable(
        fake_bin / "kill", 'printf "kill %s\\n" "$*" >> "$FAKE_CALLS"'
    )
    write_executable(
        fake_bin / "lsof", 'printf "lsof %s\\n" "$*" >> "$FAKE_CALLS"; exit 1'
    )
    write_executable(
        fake_bin / "launchctl",
        'printf "launchctl %s\\n" "$*" >> "$FAKE_CALLS"; exit 99',
    )
    return fake_bin, calls


def run_stop(
    *,
    home: Path,
    data_root: Path,
    fake_bin: Path,
    calls: Path,
    ps_command: str = "",
    ps_status: int = 0,
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
            FAKE_PS_COMMAND=ps_command,
            FAKE_PS_STATUS=str(ps_status),
            FAKE_HEALTH=health,
            FAKE_CURL_STATUS=str(curl_status),
            AUDIO_MEMORY_DATA_ROOT=str(data_root),
        ),
        capture_output=True,
        text=True,
        check=False,
    )


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
    assert assignments["AUDIO_MEMORY_MODEL_ROOT"] == str(
        (production_root / "models").resolve()
    )
    assert assignments["AUDIO_MEMORY_MODELS_WRITABLE"] == "0"
    assert assignments["AUDIO_MEMORY_KEYCHAIN_SERVICE"] == "Audio Memory Dev"
    assert assignments["AUDIO_MEMORY_PORT"] == "8766"
    assert assignments["AUDIO_MEMORY_RUNTIME_DIR"] == str(
        (project_root / ".runtime" / "dev" / "runtime").resolve()
    )
    assert assignments["AUDIO_MEMORY_PID_FILE"].endswith(
        "/runtime/audio-memory-dev.pid"
    )
    assert assignments["AUDIO_MEMORY_LOG_FILE"].endswith(
        "/runtime/audio-memory-dev.log"
    )
    assert not (project_root / ".runtime").exists()
    assert not production_root.exists()


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


def test_dev_start_refuses_an_occupied_port_without_creating_pid_or_log(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    write_executable(fake_bin / "curl", "exit 1")
    write_executable(fake_bin / "lsof", 'printf "%s\\n" "$*" >> "$FAKE_CALLS"')
    write_executable(
        fake_bin / "launchctl", 'printf "launchctl %s\\n" "$*" >> "$FAKE_CALLS"; exit 99'
    )

    result = subprocess.run(
        ["/bin/bash", str(DEV_START)],
        cwd=REPOSITORY_ROOT,
        env=isolated_environment(
            home,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_CALLS=str(calls),
            AUDIO_MEMORY_DATA_ROOT=str(data_root),
            AUDIO_MEMORY_NO_OPEN="1",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "端口 8766 已被其他程序占用" in result.stderr
    assert "launchctl" not in (calls.read_text() if calls.exists() else "")
    assert not (data_root / "runtime" / "audio-memory-dev.pid").exists()
    assert not (data_root / "runtime" / "audio-memory-dev.log").exists()


def test_dev_start_preserves_default_shared_models_as_implicit_read_only_input(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    curl_state = tmp_path / "curl-state"
    write_executable(
        fake_bin / "curl",
        'if [ ! -f "$FAKE_CURL_STATE" ]; then : > "$FAKE_CURL_STATE"; exit 1; fi\n'
        "printf '%s\\n' '{\"status\":\"ok\",\"profile\":\"development\"}'",
    )
    write_executable(fake_bin / "lsof", "exit 1")
    write_executable(
        fake_bin / "env",
        'if [ "${AUDIO_MEMORY_MODEL_ROOT+x}" = "x" ]; then\n'
        '  printf "model-root=SET\\n" >> "$FAKE_CALLS"\n'
        "else\n"
        '  printf "model-root=UNSET\\n" >> "$FAKE_CALLS"\n'
        "fi",
    )
    write_executable(
        fake_bin / "launchctl", 'printf "launchctl\\n" >> "$FAKE_CALLS"; exit 99'
    )

    result = subprocess.run(
        ["/bin/bash", str(DEV_START)],
        cwd=REPOSITORY_ROOT,
        env=isolated_environment(
            home,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_CALLS=str(calls),
            FAKE_CURL_STATE=str(curl_state),
            AUDIO_MEMORY_DATA_ROOT=str(data_root),
            AUDIO_MEMORY_NO_OPEN="1",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "model-root=UNSET"
    ]


@pytest.mark.parametrize(
    ("ps_status", "ps_command"),
    [
        (1, ""),
        (0, "/usr/bin/python unrelated_service.py --port 8766"),
    ],
)
def test_dev_stop_removes_stale_or_unrelated_pid_without_signalling(
    tmp_path: Path, ps_status: int, ps_command: str
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    pid_file = runtime / "audio-memory-dev.pid"
    pid_file.write_text("4242\n", encoding="utf-8")
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
    recorded = calls.read_text() if calls.exists() else ""
    assert "kill -TERM" not in recorded
    assert "launchctl" not in recorded


def test_dev_stop_rejects_non_numeric_pid_without_running_process_tools(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    pid_file = runtime / "audio-memory-dev.pid"
    pid_file.write_text("42; launchctl bootout\n", encoding="utf-8")
    fake_bin, calls = lifecycle_fakes(tmp_path)

    result = run_stop(
        home=home, data_root=data_root, fake_bin=fake_bin, calls=calls
    )

    assert result.returncode != 0
    assert not pid_file.exists()
    assert not calls.exists()


def test_dev_stop_requires_development_health_identity_before_term(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    pid_file = runtime / "audio-memory-dev.pid"
    pid_file.write_text("4242\n", encoding="utf-8")
    fake_bin, calls = lifecycle_fakes(tmp_path)
    expected_command = (
        f"{PYTHON} -m uvicorn audio_memory.main:app "
        f"--app-dir {REPOSITORY_ROOT / 'backend' / 'src'} "
        "--host 127.0.0.1 --port 8766"
    )

    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        ps_command=expected_command,
        health='{"status":"ok","profile":"production"}',
    )

    assert result.returncode != 0
    assert not pid_file.exists()
    recorded = calls.read_text() if calls.exists() else ""
    assert "kill -TERM" not in recorded
    assert "launchctl" not in recorded


def test_dev_stop_sends_term_only_for_matching_worktree_and_health(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "development-data"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    pid_file = runtime / "audio-memory-dev.pid"
    pid_file.write_text("4242\n", encoding="utf-8")
    fake_bin, calls = lifecycle_fakes(tmp_path)
    expected_command = (
        f"{PYTHON} -m uvicorn audio_memory.main:app "
        f"--app-dir {REPOSITORY_ROOT / 'backend' / 'src'} "
        "--host 127.0.0.1 --port 8766"
    )

    result = run_stop(
        home=home,
        data_root=data_root,
        fake_bin=fake_bin,
        calls=calls,
        ps_command=expected_command,
    )

    assert result.returncode == 0, result.stderr
    assert not pid_file.exists()
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "kill -TERM -- 4242"
    ]
