from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import hashlib
import json
import sys
import signal
import time


PROJECT_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = PROJECT_ROOT / "scripts" / "install-release.sh"


def create_release(root: Path, version: str = "0.1.0-beta.1") -> Path:
    (root / "backend").mkdir(parents=True)
    runtime_package = root / "backend" / "src" / "audio_memory"
    runtime_package.mkdir(parents=True)
    shutil.copy2(
        PROJECT_ROOT / "backend" / "src" / "audio_memory" / "__init__.py",
        runtime_package / "__init__.py",
    )
    shutil.copy2(
        PROJECT_ROOT / "backend" / "src" / "audio_memory" / "config.py",
        runtime_package / "config.py",
    )
    (root / "prototype" / "dist" / "client").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "runtime" / "ffmpeg" / "bin").mkdir(parents=True)
    (root / "runtime" / "uv").mkdir(parents=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "THIRD_PARTY_NOTICES.md").write_text("fixture notices\n", encoding="utf-8")
    (root / "backend" / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.0.0'\n")
    (root / "backend" / "uv.lock").write_text("version = 1\nrevision = 1\nrequires-python = '>=3.12'\n")
    (root / "prototype" / "dist" / "client" / "index.html").write_text("release")
    for name in (
        "audio-memory",
        "backup_data.py",
        "com.audio-memory.local.plist.template",
        "doctor.sh",
        "doctor_checks.py",
        "install-release.sh",
        "runtime_config.py",
        "start.sh",
        "verify-ffmpeg-runtime.py",
    ):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, root / "scripts" / name)
    binaries = {}
    for name in ("ffmpeg", "ffprobe"):
        path = root / "runtime" / "ffmpeg" / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        binaries[name] = {"path": f"bin/{name}", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    runtime = root / "runtime" / "ffmpeg"
    (runtime / "LICENSE.md").write_text("fixture license\n", encoding="utf-8")
    (runtime / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "ffmpeg_version": "fixture", "platform": "darwin-arm64",
        "source_url": "https://ffmpeg.org/fixture", "source_sha256": "a" * 64,
        "configure_flags": ["--disable-gpl", "--disable-nonfree"], "binaries": binaries,
    }), encoding="utf-8")
    uv = root / "runtime" / "uv" / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    return root


def run_installer(
    home: Path,
    data_root: Path | None,
    release_root: Path,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "HOME": str(home),
        "AUDIO_MEMORY_RELEASE_ROOT": str(release_root),
        "AUDIO_MEMORY_SKIP_RELEASE_SETUP": "1",
        "AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK": "1",
        "AUDIO_MEMORY_BOOTSTRAP_PYTHON": sys.executable,
    }
    if data_root is None:
        environment.pop("AUDIO_MEMORY_DATA_ROOT", None)
    else:
        environment["AUDIO_MEMORY_DATA_ROOT"] = str(data_root)
    return subprocess.run(
        ["bash", str(INSTALLER)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def run_installer_with_release_setup(
    home: Path, data_root: Path, release_root: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER)],
        env={
            **os.environ,
            "HOME": str(home),
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_RELEASE_ROOT": str(release_root),
            "AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK": "1",
            "AUDIO_MEMORY_BOOTSTRAP_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_preserves_database_creates_backup_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release")
    home.mkdir()
    data_root.mkdir()
    database = data_root / "audio-memory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE reports (title TEXT)")
        connection.execute("INSERT INTO reports VALUES ('历史报告')")

    first = run_installer(home, data_root, release_root)
    second = run_installer(home, data_root, release_root)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT title FROM reports").fetchone() == ("历史报告",)
    backups = list((data_root / "backups").glob("*/audio-memory.sqlite3"))
    assert backups
    current = data_root / "app" / "current"
    assert current.resolve() == data_root / "app" / "versions" / "0.1.0-beta.1"
    assert (current / "prototype" / "dist" / "client" / "index.html").is_file()
    assert (home / ".local" / "bin" / "audio-memory").is_symlink()


def test_release_setup_runs_in_final_version_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release", "0.1.0-beta.3")
    home.mkdir()
    (release_root / "scripts" / "install.sh").write_text(
        """#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
printf '%s\n' "$root" > "$root/setup-root.txt"
""",
        encoding="utf-8",
    )
    (release_root / "scripts" / "install.sh").chmod(0o755)

    result = run_installer_with_release_setup(home, data_root, release_root)

    target = data_root / "app" / "versions" / "0.1.0-beta.3"
    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "setup-root.txt").read_text(encoding="utf-8").strip() == str(
        target
    )
    assert (data_root / "app" / "current").resolve() == target


def test_installer_refuses_an_existing_install_lock_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release", "0.1.0-beta.3")
    existing = data_root / "app" / "versions" / "0.1.0-beta.2"
    existing.mkdir(parents=True)
    (existing / "VERSION").write_text("0.1.0-beta.2\n", encoding="utf-8")
    (data_root / "app" / "current").symlink_to(existing)
    (data_root / "app" / ".install.lock").mkdir()
    (data_root / "app" / ".install.lock" / "unexpected").write_text(
        "unsafe", encoding="utf-8"
    )
    home.mkdir()

    result = run_installer(home, data_root, release_root)

    assert result.returncode != 0
    assert "安装锁状态异常" in result.stderr
    assert (data_root / "app" / "current").resolve() == existing
    assert not (data_root / "app" / "versions" / "0.1.0-beta.3").exists()


def test_concurrent_installers_allow_only_one_writer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release", "0.1.0-beta.3")
    home.mkdir()
    (release_root / "scripts" / "install.sh").write_text(
        """#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
sleep 0.5
printf 'ready\n' > "$root/setup-finished.txt"
""",
        encoding="utf-8",
    )
    (release_root / "scripts" / "install.sh").chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(home),
        "AUDIO_MEMORY_DATA_ROOT": str(data_root),
        "AUDIO_MEMORY_RELEASE_ROOT": str(release_root),
        "AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK": "1",
        "AUDIO_MEMORY_BOOTSTRAP_PYTHON": sys.executable,
    }

    processes = [
        subprocess.Popen(
            ["bash", str(INSTALLER)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]
    return_codes = [process.returncode for process in processes]

    assert sorted(return_codes) == [0, 1], results
    assert any("另一个安装任务" in stderr for _, stderr in results), results
    target = data_root / "app" / "versions" / "0.1.0-beta.3"
    assert (target / "setup-finished.txt").read_text(encoding="utf-8") == "ready\n"
    assert not list(target.glob(".install-*"))
    assert (data_root / "app" / "current").resolve() == target


def test_installer_recovers_lock_after_owner_is_killed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release", "0.1.0-beta.3")
    home.mkdir()
    slow_setup = """#!/bin/bash
set -euo pipefail
sleep 30
"""
    (release_root / "scripts" / "install.sh").write_text(
        slow_setup, encoding="utf-8"
    )
    (release_root / "scripts" / "install.sh").chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(home),
        "AUDIO_MEMORY_DATA_ROOT": str(data_root),
        "AUDIO_MEMORY_RELEASE_ROOT": str(release_root),
        "AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK": "1",
        "AUDIO_MEMORY_BOOTSTRAP_PYTHON": sys.executable,
    }
    process = subprocess.Popen(
        ["bash", str(INSTALLER)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    lock = data_root / "app" / ".install.lock"
    for _ in range(100):
        if lock.exists() or lock.is_symlink():
            break
        time.sleep(0.02)
    assert lock.exists() or lock.is_symlink()
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=5)

    target = data_root / "app" / "versions" / "0.1.0-beta.3"
    setup_script = (
        target / "scripts" / "install.sh"
        if target.exists()
        else release_root / "scripts" / "install.sh"
    )
    setup_script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    setup_script.chmod(0o755)

    retried = run_installer_with_release_setup(home, data_root, release_root)

    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert (data_root / "app" / "current").resolve() == target
    assert not lock.exists()


def test_invalid_release_does_not_replace_current_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    existing = data_root / "app" / "versions" / "0.0.9"
    existing.mkdir(parents=True)
    (existing / "VERSION").write_text("0.0.9\n")
    (data_root / "app" / "current").symlink_to(existing)
    invalid_release = tmp_path / "invalid-release"
    invalid_release.mkdir()
    (invalid_release / "VERSION").write_text("0.1.0-beta.1\n")
    home.mkdir()

    result = run_installer(home, data_root, invalid_release)

    assert result.returncode != 0
    assert (data_root / "app" / "current").resolve() == existing


def test_installer_rejects_a_release_without_doctor_runtime_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release")
    (release_root / "scripts" / "runtime_config.py").unlink()
    home.mkdir()

    result = run_installer(home, data_root, release_root)

    assert result.returncode != 0
    assert "scripts/runtime_config.py" in result.stderr


def test_installer_rejects_a_release_without_doctor_checks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release")
    (release_root / "scripts" / "doctor_checks.py").unlink()
    home.mkdir()

    result = run_installer(home, data_root, release_root)

    assert result.returncode != 0
    assert "scripts/doctor_checks.py" in result.stderr


def test_default_install_backs_up_exact_production_database_before_switching_current(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data_root = home / "Library" / "Application Support" / "AudioMemory"
    first_release = create_release(tmp_path / "first-release", "0.1.0-beta.1")
    first = run_installer(home, None, first_release)
    assert first.returncode == 0, first.stdout + first.stderr

    database = data_root / "audio-memory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE reports (title TEXT)")
        connection.execute("INSERT INTO reports VALUES ('历史报告')")

    next_release = create_release(tmp_path / "next-release", "0.1.0-beta.2")
    (next_release / "scripts" / "backup_data.py").write_text(
        """from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
current = source.parent / "app" / "current"
if current.resolve().name != "0.1.0-beta.1":
    raise SystemExit(73)
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(source, destination)
(destination.parent / "current-before-backup.txt").write_text(
    str(current.resolve()), encoding="utf-8"
)
""",
        encoding="utf-8",
    )

    second = run_installer(home, None, next_release)

    assert second.returncode == 0, second.stdout + second.stderr
    assert database == (
        home / "Library" / "Application Support" / "AudioMemory" / "audio-memory.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT title FROM reports").fetchone() == ("历史报告",)
    backup_directories = sorted((data_root / "backups").iterdir())
    latest_backup = backup_directories[-1]
    assert (latest_backup / "audio-memory.sqlite3").is_file()
    assert (latest_backup / "current-before-backup.txt").read_text(
        encoding="utf-8"
    ).endswith("/0.1.0-beta.1")
    assert (data_root / "app" / "current").resolve().name == "0.1.0-beta.2"


def test_installed_doctor_can_resolve_its_packaged_runtime_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    release_root = create_release(tmp_path / "release")
    home.mkdir()

    installation = run_installer(home, data_root, release_root)
    doctor = data_root / "app" / "current" / "scripts" / "doctor.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").write_text(
        "#!/bin/bash\nexec \"$TEST_RUNTIME_PYTHON\" \"$@\"\n",
        encoding="utf-8",
    )
    (fake_bin / "python3").chmod(0o755)
    result = subprocess.run(
        ["bash", str(doctor)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TEST_RUNTIME_PYTHON": str(
                PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"
            ),
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_DOCTOR_CORE_ONLY": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert installation.returncode == 0, installation.stdout + installation.stderr
    assert "运行配置：profile=production port=8765 data=production" in result.stdout


def test_doctor_uses_the_release_python_for_packaged_checks() -> None:
    doctor = (PROJECT_ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")

    assert "check '模型清单' python3" not in doctor
    assert "check 'Whisper 模型清单' \"$PYTHON_BIN\"" in doctor
    assert "check '本地数据库已迁移至 0014' \"$PYTHON_BIN\"" in doctor


def test_tampered_ffmpeg_runtime_does_not_replace_current_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data_root = tmp_path / "data"
    existing = data_root / "app" / "versions" / "0.0.9"
    existing.mkdir(parents=True)
    (existing / "VERSION").write_text("0.0.9\n")
    (data_root / "app" / "current").symlink_to(existing)
    release = create_release(tmp_path / "release")
    with (release / "runtime" / "ffmpeg" / "bin" / "ffprobe").open("ab") as handle:
        handle.write(b"tampered")
    home.mkdir()

    result = run_installer(home, data_root, release)

    assert result.returncode != 0
    assert "FFmpeg runtime verification failed" in result.stderr
    assert (data_root / "app" / "current").resolve() == existing
