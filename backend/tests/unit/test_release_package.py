from __future__ import annotations

import os
from pathlib import Path
import hashlib
import shutil
import subprocess
import tarfile
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILDER = PROJECT_ROOT / "scripts" / "build-release.sh"


def test_release_archive_uses_runtime_whitelist(tmp_path: Path) -> None:
    contamination = (
        PROJECT_ROOT
        / "prototype"
        / "dist"
        / "client"
        / f".release-exclusion-fixture-{uuid4()}"
    )
    dependency_target = tmp_path / "local-dependency"
    dependency_target.mkdir()
    contamination.mkdir()
    fixture_files = (
        ".runtime/state.json",
        ".env.production",
        "state.sqlite3",
        "state.sqlite3-wal",
        "state.sqlite3-shm",
        "state.sqlite3-journal",
        "UPPER-STATE.SQLITE3-WAL",
        "recording.mp3",
        "UPPER-RECORDING.MP3",
        "recording.flac",
        "server.log",
        "UPPER-SERVER.LOG",
        "server.log.1",
        "models/weights.onnx",
        "node_modules/package/index.js",
        ".uv-cache/package/artifact",
        ".pytest_cache/state.json",
        ".mypy_cache/state.json",
        ".ruff_cache/state.json",
        "fixture.egg-info/PKG-INFO",
        "tests/fixture.json",
        "outputs/report.json",
        "build/temporary.bin",
        "__pycache__/cache.pyc",
    )
    for relative_path in fixture_files:
        path = contamination / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not ship\n", encoding="utf-8")
    (contamination / "linked-dependency").symlink_to(
        dependency_target, target_is_directory=True
    )

    try:
        result = subprocess.run(
            ["bash", str(BUILDER)],
            cwd=PROJECT_ROOT,
            env={
                **os.environ,
                "AUDIO_MEMORY_RELEASE_DIST": str(tmp_path),
                "AUDIO_MEMORY_ALLOW_DIRTY_RELEASE": "1",
                "AUDIO_MEMORY_SKIP_RELEASE_BUILD": "1",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(contamination)

    assert result.returncode == 0, result.stdout + result.stderr
    archive = tmp_path / "audio-memory-v0.1.0-beta.1-macos-arm64.tar.gz"
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    assert archive.is_file()
    assert checksum.is_file()
    with tarfile.open(archive) as handle:
        members = handle.getmembers()
        names = {member.name for member in members}
    prefix = "audio-memory-v0.1.0-beta.1"
    required = {
        f"{prefix}/VERSION",
        f"{prefix}/backend/pyproject.toml",
        f"{prefix}/backend/src/audio_memory/main.py",
        f"{prefix}/backend/migrations/versions/0014_app_settings.py",
        f"{prefix}/prototype/dist/client/index.html",
        f"{prefix}/scripts/audio-memory",
        f"{prefix}/scripts/backup_data.py",
        f"{prefix}/scripts/com.audio-memory.local.plist.template",
        f"{prefix}/scripts/doctor.sh",
        f"{prefix}/scripts/doctor_checks.py",
        f"{prefix}/scripts/install-release.sh",
        f"{prefix}/scripts/install.sh",
        f"{prefix}/scripts/runtime_config.py",
        f"{prefix}/scripts/start.sh",
    }
    assert required <= names
    forbidden_parts = {
        ".git",
        ".venv",
        ".runtime",
        ".uv-cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "tests",
        "outputs",
        "screenshots",
        "designs",
        "models",
        "audio",
        "build",
        "audio-memory.sqlite3",
        ".env",
        "__pycache__",
    }
    assert not any(forbidden_parts & set(Path(name).parts) for name in names)
    assert not any(
        part.endswith(".egg-info")
        for name in names
        for part in Path(name).parts
    )
    assert not any(
        Path(name).name.startswith(".env")
        or name.casefold().endswith(
            (
                ".pyc",
                ".pyo",
                ".sqlite",
                ".sqlite3",
                ".sqlite-wal",
                ".sqlite-shm",
                ".sqlite-journal",
                ".sqlite3-wal",
                ".sqlite3-shm",
                ".sqlite3-journal",
                ".db",
                ".db-wal",
                ".db-shm",
                ".db-journal",
                ".mp3",
                ".aac",
                ".m4a",
                ".wav",
                ".flac",
                ".ogg",
                ".opus",
                ".wma",
                ".caf",
                ".aiff",
                ".log",
            )
        )
        or ".log." in Path(name).name.casefold()
        for name in names
    )
    assert not any(member.issym() or member.islnk() for member in members)
    expected_hash = checksum.read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_hash
