from pathlib import Path

from audio_memory import __version__


def test_release_version_matches_backend_version() -> None:
    project_root = Path(__file__).resolve().parents[3]
    release_version = (project_root / "VERSION").read_text(encoding="utf-8").strip()

    assert release_version == "0.1.0-beta.3"
    assert __version__ == release_version
