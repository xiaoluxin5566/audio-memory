from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path


WHISPER_MODEL_ID = "mlx-community/whisper-large-v3-turbo"


class UnsupportedPlatformError(RuntimeError):
    """Raised when the backend is started outside the phase-one platform."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    runtime: Path
    lock: Path
    feedback: Path
    staging: Path
    audio: Path
    prompts: Path

    @classmethod
    def from_home(cls, home: Path) -> "AppPaths":
        root = home / "Library" / "Application Support" / "AudioMemory"
        runtime = root / "runtime"
        return cls(
            root=root,
            database=root / "audio-memory.sqlite3",
            runtime=runtime,
            lock=runtime / "audio-memory.lock",
            feedback=root / "意见反馈",
            staging=root / "staging",
            audio=root / "audio",
            prompts=root / "prompts",
        )

    @property
    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.runtime,
            self.feedback,
            self.staging,
            self.audio,
            self.prompts,
        )

    def ensure_directories(self) -> None:
        for directory in self.required_directories:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)


def assert_supported_platform() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise UnsupportedPlatformError(
            "Audio Memory 第一阶段仅支持 macOS Apple Silicon。"
        )
