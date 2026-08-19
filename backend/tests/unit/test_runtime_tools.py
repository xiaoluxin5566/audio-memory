from __future__ import annotations

from pathlib import Path

import pytest

from audio_memory.runtime_tools import RuntimeToolUnavailable, resolve_runtime_tool


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_explicit_runtime_tool_path_wins(monkeypatch, tmp_path: Path) -> None:
    explicit = executable(tmp_path / "custom" / "ffprobe")
    monkeypatch.setenv("AUDIO_MEMORY_FFPROBE", str(explicit))

    assert resolve_runtime_tool("ffprobe") == str(explicit)


def test_release_resolves_tool_from_bundled_runtime(monkeypatch, tmp_path: Path) -> None:
    bundled = executable(tmp_path / "runtime" / "ffmpeg" / "bin" / "ffmpeg")
    monkeypatch.setenv("AUDIO_MEMORY_RELEASE_ROOT", str(tmp_path))
    monkeypatch.setenv("AUDIO_MEMORY_RELEASE_MODE", "1")
    monkeypatch.delenv("AUDIO_MEMORY_FFMPEG", raising=False)

    assert resolve_runtime_tool("ffmpeg") == str(bundled)


def test_release_never_falls_back_to_system_path(monkeypatch, tmp_path: Path) -> None:
    system = executable(tmp_path / "system" / "ffprobe")
    monkeypatch.setenv("PATH", str(system.parent))
    monkeypatch.setenv("AUDIO_MEMORY_RELEASE_ROOT", str(tmp_path / "missing"))
    monkeypatch.setenv("AUDIO_MEMORY_RELEASE_MODE", "1")
    monkeypatch.delenv("AUDIO_MEMORY_FFPROBE", raising=False)

    with pytest.raises(RuntimeToolUnavailable, match="ffprobe"):
        resolve_runtime_tool("ffprobe")


def test_source_development_can_use_system_path(monkeypatch, tmp_path: Path) -> None:
    system = executable(tmp_path / "system" / "ffmpeg")
    monkeypatch.setenv("PATH", str(system.parent))
    monkeypatch.delenv("AUDIO_MEMORY_RELEASE_MODE", raising=False)
    monkeypatch.delenv("AUDIO_MEMORY_RELEASE_ROOT", raising=False)
    monkeypatch.delenv("AUDIO_MEMORY_FFMPEG", raising=False)

    assert resolve_runtime_tool("ffmpeg") == str(system)
