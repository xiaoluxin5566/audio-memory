#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


class VerificationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise VerificationError(f"manifest field {key} is invalid")
    return value


def verify_runtime(root: Path) -> None:
    root = root.resolve()
    manifest_path = root / "manifest.json"
    license_path = root / "LICENSE.md"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerificationError("FFmpeg manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise VerificationError("FFmpeg manifest schema is unsupported")
    if not license_path.is_file() or license_path.stat().st_size == 0:
        raise VerificationError("FFmpeg LICENSE.md is missing")
    if require_string(manifest, "platform") != "darwin-arm64":
        raise VerificationError("FFmpeg runtime must target darwin-arm64")
    require_string(manifest, "ffmpeg_version")
    require_string(manifest, "source_url")
    source_hash = require_string(manifest, "source_sha256")
    if len(source_hash) != 64:
        raise VerificationError("FFmpeg source SHA-256 is invalid")
    flags = manifest.get("configure_flags")
    if not isinstance(flags, list) or not all(isinstance(item, str) for item in flags):
        raise VerificationError("FFmpeg configure_flags are invalid")
    if "--disable-gpl" not in flags or "--disable-nonfree" not in flags:
        raise VerificationError("FFmpeg runtime must disable GPL and nonfree components")
    binaries = manifest.get("binaries")
    if not isinstance(binaries, dict):
        raise VerificationError("FFmpeg binary manifest is invalid")

    for name in ("ffmpeg", "ffprobe"):
        item = binaries.get(name)
        if not isinstance(item, dict):
            raise VerificationError(f"{name} manifest entry is missing")
        relative = Path(require_string(item, "path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise VerificationError(f"{name} path is unsafe")
        binary = (root / relative).resolve()
        if not binary.is_relative_to(root) or not binary.is_file():
            raise VerificationError(f"{name} binary is missing")
        if not binary.stat().st_mode & 0o111:
            raise VerificationError(f"{name} binary is not executable")
        expected_hash = require_string(item, "sha256")
        if len(expected_hash) != 64 or sha256(binary) != expected_hash:
            raise VerificationError(f"{name} SHA-256 does not match manifest")
        if os.environ.get("AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK") != "1":
            architecture = subprocess.run(
                ["/usr/bin/file", str(binary)], capture_output=True, text=True, check=False
            )
            if architecture.returncode != 0 or "arm64" not in architecture.stdout:
                raise VerificationError(f"{name} is not an Apple Silicon executable")
        version = subprocess.run(
            [str(binary), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if version.returncode != 0:
            raise VerificationError(f"{name} cannot execute")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify-ffmpeg-runtime.py RUNTIME_ROOT", file=sys.stderr)
        return 2
    try:
        verify_runtime(Path(sys.argv[1]))
    except (OSError, subprocess.SubprocessError, VerificationError) as exc:
        print(f"FFmpeg runtime verification failed: {exc}", file=sys.stderr)
        return 1
    print("FFmpeg runtime verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
