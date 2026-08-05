from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioProbe:
    duration_ms: int
    codec_name: str


async def probe_audio(path: Path) -> AudioProbe | None:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name:format=duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(stdout)
        streams = payload["streams"]
        duration = float(payload["format"]["duration"])
        codec = str(streams[0]["codec_name"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return AudioProbe(duration_ms=max(1, round(duration * 1000)), codec_name=codec)


def supports(extension: str, probe: AudioProbe | None) -> bool:
    if probe is None:
        return False
    if extension == ".mp3":
        return probe.codec_name == "mp3"
    if extension == ".aac":
        return probe.codec_name == "aac"
    return False

