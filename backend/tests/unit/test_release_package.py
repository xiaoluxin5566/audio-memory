from __future__ import annotations

import os
from pathlib import Path
import hashlib
import subprocess
import tarfile


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILDER = PROJECT_ROOT / "scripts" / "build-release.sh"


def test_release_archive_uses_runtime_whitelist(tmp_path: Path) -> None:
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

    assert result.returncode == 0, result.stdout + result.stderr
    archive = tmp_path / "audio-memory-v0.1.0-beta.1-macos-arm64.tar.gz"
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    assert archive.is_file()
    assert checksum.is_file()
    with tarfile.open(archive) as handle:
        names = set(handle.getnames())
    prefix = "audio-memory-v0.1.0-beta.1"
    required = {
        f"{prefix}/VERSION",
        f"{prefix}/backend/pyproject.toml",
        f"{prefix}/backend/src/audio_memory/main.py",
        f"{prefix}/backend/migrations/versions/0014_app_settings.py",
        f"{prefix}/prototype/dist/client/index.html",
        f"{prefix}/scripts/audio-memory",
        f"{prefix}/scripts/install-release.sh",
    }
    assert required <= names
    forbidden_parts = {
        ".git",
        ".venv",
        "node_modules",
        "tests",
        "outputs",
        "screenshots",
        "designs",
        "audio-memory.sqlite3",
        ".env",
        "__pycache__",
    }
    assert not any(forbidden_parts & set(Path(name).parts) for name in names)
    assert not any(name.endswith((".pyc", ".pyo")) for name in names)
    expected_hash = checksum.read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_hash
