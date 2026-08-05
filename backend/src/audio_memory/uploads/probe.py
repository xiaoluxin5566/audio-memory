from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioProbe:
    duration_ms: int
    codec_name: str
    creation_time: str | None = None


async def probe_audio(path: Path) -> AudioProbe | None:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name:stream_tags=creation_time:format=duration:format_tags=creation_time",
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
    embedded_time = payload["format"].get("tags", {}).get("creation_time")
    if embedded_time is None:
        embedded_time = streams[0].get("tags", {}).get("creation_time")
    return AudioProbe(
        duration_ms=max(1, round(duration * 1000)),
        codec_name=codec,
        creation_time=normalize_creation_time(embedded_time),
    )


def normalize_creation_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def supports(extension: str, probe: AudioProbe | None) -> bool:
    if probe is None:
        return False
    if extension == ".mp3":
        return probe.codec_name == "mp3"
    if extension == ".aac":
        return probe.codec_name == "aac"
    return False
