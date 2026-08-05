from pathlib import Path

import pytest

from audio_memory.uploads.cleanup import UnsafeCleanupPathError, assert_staging_path


def test_cleanup_accepts_only_descendants_of_staging(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()

    assert assert_staging_path(staging / "job" / "audio.mp3", staging) == (
        staging / "job" / "audio.mp3"
    )
    with pytest.raises(UnsafeCleanupPathError):
        assert_staging_path(tmp_path / "user-audio.mp3", staging)
    with pytest.raises(UnsafeCleanupPathError):
        assert_staging_path(staging, staging)
