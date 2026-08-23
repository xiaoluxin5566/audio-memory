from __future__ import annotations

import pytest

from audio_memory.asr.files import AsrFileCandidate, AsrFileError, validate_asr_file


def candidate(**overrides) -> AsrFileCandidate:
    values = {
        "relative_path": "staging/job/file.mp3",
        "extension": ".mp3",
        "codec_name": "mp3",
        "size_bytes": 1024,
        "duration_ms": 60_000,
        "sha256": "a" * 64,
    }
    values.update(overrides)
    return AsrFileCandidate(**values)


def test_supported_small_mp3_is_not_transcoded() -> None:
    submission = validate_asr_file(candidate())
    assert submission.relative_path == "staging/job/file.mp3"
    assert submission.transcoded is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"duration_ms": 5 * 60 * 60 * 1000},
        {"size_bytes": 512 * 1024 * 1024},
    ],
)
def test_limit_is_rejected_before_remote_submission(overrides) -> None:
    with pytest.raises(AsrFileError) as caught:
        validate_asr_file(candidate(**overrides))
    assert caught.value.code == "file_exceeds_asr_limit"


@pytest.mark.parametrize(
    "overrides",
    [
        {"relative_path": "/tmp/file.mp3"},
        {"relative_path": "../file.mp3"},
        {"extension": ".wav", "codec_name": "pcm_s16le"},
        {"extension": ".mp3", "codec_name": "aac"},
    ],
)
def test_unsafe_or_mismatched_file_is_rejected(overrides) -> None:
    with pytest.raises(AsrFileError):
        validate_asr_file(candidate(**overrides))

