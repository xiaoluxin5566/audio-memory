from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import hashlib
import json


PROJECT_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = PROJECT_ROOT / "scripts" / "install-release.sh"


def create_release(root: Path, version: str = "0.1.0-beta.1") -> Path:
    (root / "backend").mkdir(parents=True)
    (root / "prototype" / "dist" / "client").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "runtime" / "ffmpeg" / "bin").mkdir(parents=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "backend" / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.0.0'\n")
    (root / "backend" / "uv.lock").write_text("version = 1\nrevision = 1\nrequires-python = '>=3.12'\n")
    (root / "prototype" / "dist" / "client" / "index.html").write_text("release")
    for name in (
        "audio-memory",
        "backup_data.py",
        "com.audio-memory.local.plist.template",
        "doctor.sh",
        "install-release.sh",
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
    return root


def run_installer(home: Path, data_root: Path, release_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER)],
        env={
            **os.environ,
            "HOME": str(home),
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_RELEASE_ROOT": str(release_root),
            "AUDIO_MEMORY_SKIP_RELEASE_SETUP": "1",
            "AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK": "1",
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
