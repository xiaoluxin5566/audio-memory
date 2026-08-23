from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from audio_memory.asr.types import ASR_PROVIDER_CONFIGS, AsrProviderId


class AsrFileError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AsrFileCandidate:
    relative_path: str
    extension: str
    codec_name: str
    size_bytes: int
    duration_ms: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AsrFileSubmission:
    relative_path: str
    extension: str
    size_bytes: int
    duration_ms: int
    sha256: str
    transcoded: bool = False


def validate_asr_file(candidate: AsrFileCandidate) -> AsrFileSubmission:
    config = ASR_PROVIDER_CONFIGS[AsrProviderId.VOLCANO]
    path = PurePosixPath(candidate.relative_path)
    if (
        not candidate.relative_path
        or path.is_absolute()
        or ".." in path.parts
        or candidate.extension not in config.supported_extensions
    ):
        raise AsrFileError("unsupported_format")
    expected_codec = {".mp3": "mp3", ".aac": "aac"}[candidate.extension]
    if candidate.codec_name != expected_codec:
        raise AsrFileError("unsupported_format")
    if candidate.duration_ms <= 0 or candidate.size_bytes <= 0:
        raise AsrFileError("invalid_audio")
    if (
        candidate.duration_ms >= config.max_duration_ms
        or candidate.size_bytes >= config.max_size_bytes
    ):
        raise AsrFileError("file_exceeds_asr_limit")
    if len(candidate.sha256) != 64 or any(
        character not in "0123456789abcdef" for character in candidate.sha256
    ):
        raise AsrFileError("invalid_file_hash")
    return AsrFileSubmission(
        relative_path=candidate.relative_path,
        extension=candidate.extension,
        size_bytes=candidate.size_bytes,
        duration_ms=candidate.duration_ms,
        sha256=candidate.sha256,
    )

