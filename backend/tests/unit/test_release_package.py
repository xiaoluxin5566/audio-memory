from __future__ import annotations

import os
from pathlib import Path
import hashlib
import subprocess
import tarfile
import json


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILDER = PROJECT_ROOT / "scripts" / "build-release.sh"


def create_runtime(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    binaries = {}
    for name in ("ffmpeg", "ffprobe"):
        path = root / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        binaries[name] = {
            "path": f"bin/{name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (root / "LICENSE.md").write_text("fixture license\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "ffmpeg_version": "fixture",
        "platform": "darwin-arm64", "source_url": "https://ffmpeg.org/fixture",
        "source_sha256": "a" * 64,
        "configure_flags": ["--disable-gpl", "--disable-nonfree"],
        "binaries": binaries,
    }), encoding="utf-8")
    return root


def test_release_archive_uses_runtime_whitelist(tmp_path: Path) -> None:
    runtime = create_runtime(tmp_path / "ffmpeg-runtime")
    result = subprocess.run(
        ["bash", str(BUILDER)],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "AUDIO_MEMORY_RELEASE_DIST": str(tmp_path),
            "AUDIO_MEMORY_ALLOW_DIRTY_RELEASE": "1",
            "AUDIO_MEMORY_SKIP_RELEASE_BUILD": "1",
            "AUDIO_MEMORY_FFMPEG_RUNTIME": str(runtime),
            "AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK": "1",
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
        f"{prefix}/scripts/verify-ffmpeg-runtime.py",
        f"{prefix}/runtime/ffmpeg/bin/ffmpeg",
        f"{prefix}/runtime/ffmpeg/bin/ffprobe",
        f"{prefix}/runtime/ffmpeg/manifest.json",
        f"{prefix}/runtime/ffmpeg/LICENSE.md",
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
