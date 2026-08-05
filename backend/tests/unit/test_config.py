from pathlib import Path

import pytest

from audio_memory.config import (
    AppPaths,
    UnsupportedPlatformError,
    assert_supported_platform,
)


def test_app_paths_are_all_under_local_application_support(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)

    root = tmp_path / "Library" / "Application Support" / "AudioMemory"
    assert paths.root == root
    assert paths.database == root / "audio-memory.sqlite3"
    assert paths.runtime == root / "runtime"
    assert paths.lock == root / "runtime" / "audio-memory.lock"
    assert paths.feedback == root / "意见反馈"
    assert paths.staging == root / "staging"


def test_ensure_directories_creates_private_local_directories(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)

    paths.ensure_directories()

    for directory in paths.required_directories:
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700


def test_platform_guard_rejects_non_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("audio_memory.config.platform.system", lambda: "Darwin")
    monkeypatch.setattr("audio_memory.config.platform.machine", lambda: "x86_64")

    with pytest.raises(UnsupportedPlatformError, match="Apple Silicon"):
        assert_supported_platform()


def test_platform_guard_accepts_macos_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("audio_memory.config.platform.system", lambda: "Darwin")
    monkeypatch.setattr("audio_memory.config.platform.machine", lambda: "arm64")

    assert_supported_platform()

