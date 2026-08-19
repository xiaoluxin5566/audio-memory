from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Literal


RuntimeToolName = Literal["ffmpeg", "ffprobe"]


class RuntimeToolUnavailable(RuntimeError):
    def __init__(self, name: RuntimeToolName) -> None:
        super().__init__(f"Required audio runtime tool is unavailable: {name}")
        self.name = name


def _usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_runtime_tool(name: RuntimeToolName) -> str:
    explicit = os.environ.get(f"AUDIO_MEMORY_{name.upper()}")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if _usable(path):
            return str(path)
        raise RuntimeToolUnavailable(name)

    release_root_value = os.environ.get("AUDIO_MEMORY_RELEASE_ROOT")
    if release_root_value:
        bundled = (
            Path(release_root_value).expanduser().resolve()
            / "runtime"
            / "ffmpeg"
            / "bin"
            / name
        )
        if _usable(bundled):
            return str(bundled)

    if os.environ.get("AUDIO_MEMORY_RELEASE_MODE") == "1":
        raise RuntimeToolUnavailable(name)
    system = shutil.which(name)
    if system:
        return system
    raise RuntimeToolUnavailable(name)
