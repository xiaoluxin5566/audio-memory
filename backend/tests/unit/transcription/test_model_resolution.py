from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from audio_memory.config import AppPaths, AppProfile, RuntimeConfigurationError
from audio_memory.transcription.engine import MLXWhisperEngine, _transcribe_worker


def write_whisper_manifest(model_root: Path, snapshot: Path) -> Path:
    snapshot.mkdir(parents=True)
    config = snapshot / "config.json"
    weights = snapshot / "weights.safetensors"
    config.write_text(json.dumps({"model_type": "whisper"}), encoding="utf-8")
    weights.write_bytes(b"local-test-weights")
    manifest = model_root.parent / "whisper-model-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "model_id": "mlx-community/whisper-large-v3-turbo",
                "snapshot": str(snapshot),
                "files": [
                    {"path": "config.json"},
                    {"path": "weights.safetensors"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_read_only_engine_resolves_installed_snapshot_without_cache_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "development-data"
    model_root = tmp_path / "production-data/models"
    snapshot = tmp_path / "huggingface/hub/models--whisper/snapshots/revision"
    manifest = write_whisper_manifest(model_root, snapshot)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    download_calls: list[dict[str, object]] = []
    transcribe_calls: list[dict[str, object]] = []

    def snapshot_download(**kwargs: object) -> str:
        download_calls.append(kwargs)
        raise AssertionError("read-only development must not download a model")

    def transcribe(audio_path: str, **kwargs: object) -> dict[str, object]:
        transcribe_calls.append({"audio_path": audio_path, **kwargs})
        return {"segments": [], "language": "zh"}

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=transcribe),
    )
    paths = AppPaths.from_roots(data_root, model_root, models_writable=False)
    engine = MLXWhisperEngine(object(), paths, voice_activity_detector=object())

    reference = engine.resolve_model_reference()
    _transcribe_worker("audio.wav", reference)

    assert reference == str(snapshot.resolve())
    assert transcribe_calls[0]["path_or_hf_repo"] == str(snapshot.resolve())
    assert download_calls == []
    assert not data_root.exists()
    assert manifest.is_file()
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_read_only_engine_fails_closed_when_installed_snapshot_is_missing(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "development-data"
    model_root = tmp_path / "production-data/models"
    paths = AppPaths.from_roots(data_root, model_root, models_writable=False)
    engine = MLXWhisperEngine(object(), paths, voice_activity_detector=object())

    with pytest.raises(RuntimeConfigurationError, match="Whisper"):
        engine.resolve_model_reference()

    assert not data_root.exists()
    assert not model_root.exists()


def test_read_only_engine_never_falls_back_to_an_uninstalled_repository_id(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_roots(
        tmp_path / "development-data",
        tmp_path / "production-data/models",
        models_writable=False,
    )
    engine = MLXWhisperEngine(
        object(),
        paths,
        model_id="example/not-installed",
        voice_activity_detector=object(),
    )

    with pytest.raises(RuntimeConfigurationError, match="Whisper"):
        engine.resolve_model_reference()

    assert not paths.root.exists()
    assert not paths.models.exists()


def test_writable_development_download_cache_is_scoped_to_its_model_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "development-data"
    model_root = data_root / "models"
    snapshot = model_root / "models--whisper/snapshots/revision"
    snapshot.mkdir(parents=True)
    captured: dict[str, object] = {}

    def snapshot_download(**kwargs: object) -> str:
        captured.update(kwargs)
        return str(snapshot)

    def transcribe(_audio_path: str, **kwargs: object) -> dict[str, object]:
        captured["path_or_hf_repo"] = kwargs["path_or_hf_repo"]
        return {"segments": [], "language": "zh"}

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=transcribe),
    )
    paths = AppPaths.from_roots(data_root, model_root, models_writable=True)
    engine = MLXWhisperEngine(
        object(),
        paths,
        runtime_profile=AppProfile.DEVELOPMENT,
        voice_activity_detector=object(),
    )

    _transcribe_worker(
        "audio.wav",
        engine.resolve_model_reference(),
        model_cache_root=str(engine.model_cache_root),
    )

    assert captured["repo_id"] == "mlx-community/whisper-large-v3-turbo"
    assert Path(str(captured["cache_dir"])).resolve() == model_root.resolve()
    assert captured["path_or_hf_repo"] == str(snapshot)
    assert Path(str(captured["cache_dir"])).is_relative_to(data_root)
