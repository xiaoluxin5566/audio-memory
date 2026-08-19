from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_memory.config import (
    PinnedDevelopmentRoot,
    RuntimeConfig,
    UnsafeDevelopmentPathError,
)

from audio_memory.transcription.physical_checkpoints import (
    load_physical_chunk_checkpoint,
    physical_checkpoint_fingerprint,
    save_physical_chunk_checkpoint,
)


def test_physical_checkpoint_round_trip_is_fingerprint_bound(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    path = staging / "job-1" / "batch-2-part-0.json"
    fingerprint = physical_checkpoint_fingerprint(
        audio_fingerprint="a" * 64,
        model_id="mlx-community/whisper-large-v3-turbo",
        parameters_fingerprint="b" * 64,
        batch_index=2,
    )
    segments = [{"start": 1.0, "end": 2.0, "text": "hello"}]

    save_physical_chunk_checkpoint(
        path,
        staging_root=staging,
        fingerprint=fingerprint,
        part_index=0,
        segments=segments,
        language="zh",
        language_confidence=0.95,
    )

    restored = load_physical_chunk_checkpoint(
        path,
        staging_root=staging,
        expected_fingerprint=fingerprint,
        expected_part_index=0,
    )
    assert restored == {
        "segments": segments,
        "language": "zh",
        "language_confidence": 0.95,
    }
    assert load_physical_chunk_checkpoint(
        path,
        staging_root=staging,
        expected_fingerprint="c" * 64,
        expected_part_index=0,
    ) is None


def test_malformed_physical_checkpoint_is_ignored(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    path = staging / "broken.json"
    path.write_text("{broken", encoding="utf-8")

    assert load_physical_chunk_checkpoint(
        path,
        staging_root=staging,
        expected_fingerprint="a" * 64,
        expected_part_index=0,
    ) is None


def test_physical_checkpoint_write_leaves_no_temporary_sibling(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    path = staging / "job" / "part.json"

    save_physical_chunk_checkpoint(
        path,
        staging_root=staging,
        fingerprint="a" * 64,
        part_index=0,
        segments=[],
        language=None,
        language_confidence=None,
    )

    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert list(path.parent.glob("*.tmp")) == []


def test_development_checkpoint_rejects_nested_staging_symlink(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    boundary = PinnedDevelopmentRoot.open(config, create=True)
    assert boundary is not None
    boundary.ensure_directories()
    protected = tmp_path / "production-checkpoints"
    protected.mkdir()
    nested = config.paths.staging / "job-1"
    nested.symlink_to(protected, target_is_directory=True)

    try:
        with pytest.raises(UnsafeDevelopmentPathError):
            save_physical_chunk_checkpoint(
                nested / "part.json",
                staging_root=config.paths.staging,
                write_boundary=boundary,
                fingerprint="a" * 64,
                part_index=0,
                segments=[],
                language=None,
                language_confidence=None,
            )
    finally:
        boundary.close()

    assert not any(protected.iterdir())
