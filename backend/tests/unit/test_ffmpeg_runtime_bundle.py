from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[3]
VERIFIER = PROJECT_ROOT / "scripts" / "verify-ffmpeg-runtime.py"


def create_runtime(root: Path) -> Path:
    binary_root = root / "bin"
    binary_root.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for name in ("ffmpeg", "ffprobe"):
        target = binary_root / name
        target.write_text("#!/bin/sh\nprintf '%s fixture\\n' \"$0\"\n", encoding="utf-8")
        target.chmod(0o755)
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (root / "LICENSE.md").write_text("FFmpeg LGPL runtime fixture\n")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ffmpeg_version": "fixture",
                "platform": "darwin-arm64",
                "source_url": "https://ffmpeg.org/releases/ffmpeg-fixture.tar.xz",
                "source_sha256": "a" * 64,
                "configure_flags": ["--disable-gpl", "--disable-nonfree"],
                "binaries": {
                    name: {"path": f"bin/{name}", "sha256": digest}
                    for name, digest in hashes.items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def verify(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.environ.get("PYTHON", "python3"), str(VERIFIER), str(root)],
        env={**os.environ, "AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK": "1"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_verifier_accepts_complete_manifest_backed_arm64_runtime(tmp_path: Path) -> None:
    result = verify(create_runtime(tmp_path / "runtime"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FFmpeg runtime verified" in result.stdout


def test_verifier_rejects_missing_ffprobe(tmp_path: Path) -> None:
    runtime = create_runtime(tmp_path / "runtime")
    (runtime / "bin" / "ffprobe").unlink()

    result = verify(runtime)

    assert result.returncode != 0
    assert "ffprobe" in result.stderr


def test_verifier_rejects_tampered_binary(tmp_path: Path) -> None:
    runtime = create_runtime(tmp_path / "runtime")
    with (runtime / "bin" / "ffmpeg").open("ab") as handle:
        handle.write(b"tampered")

    result = verify(runtime)

    assert result.returncode != 0
    assert "SHA-256" in result.stderr
