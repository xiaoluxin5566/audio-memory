from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
import sys
import wave

import pytest

from audio_memory.config import (
    AppPaths,
    AppProfile,
    PinnedDevelopmentRoot,
    RuntimeConfig,
    UnsafeDevelopmentPathError,
)
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, JobFile
from audio_memory.transcription.compact import CompactBatch, CompactEntry
from audio_memory.transcription.engine import (
    MLXWhisperEngine,
    SpeechInterval,
    WhisperBatchResult,
    _transcribe_worker,
    prepare_compact_wav,
)


def _batch() -> CompactBatch:
    return CompactBatch(
        index=0,
        entries=(
            CompactEntry(0, 1_000, 10_000, 11_000, "source", 10_000, 11_000),
            CompactEntry.separator(1_000, 1_500),
            CompactEntry(1_500, 2_500, 30_000, 31_000, "source", 30_000, 31_000),
        ),
        speech_ms=2_000,
        compact_ms=2_500,
        forced_split=False,
        parameter_fingerprint="fingerprint",
    )


async def _development_transcription_fixture(tmp_path: Path):
    data_root = tmp_path / "project/.runtime/dev"
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        environ={
            "AUDIO_MEMORY_PROFILE": "development",
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_MODEL_ROOT": str(data_root / "models"),
        },
    )
    boundary = PinnedDevelopmentRoot.open(config, create=True)
    assert boundary is not None
    boundary.ensure_directories()
    database = Database(config.paths.database, write_boundary=boundary)
    await database.create_schema()
    source = config.paths.staging / "job-1/source.mp3"
    boundary.write_bytes_atomic(source, b"synthetic")
    job_file = JobFile(
        id="file-1",
        job_id="job-1",
        original_name="source.mp3",
        extension=".mp3",
        size_bytes=9,
        sha256="a" * 64,
        duration_ms=1_000,
        position=0,
        temporary_path=str(source),
    )
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(job_file)
        await session.commit()
    return config, boundary, database, job_file


def test_prepare_compact_wav_uses_ordered_source_clips_and_synthetic_separator(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("audio_memory.transcription.engine.subprocess.run", run)
    target = tmp_path / "batch.wav"

    assert prepare_compact_wav(tmp_path / "private-source.mp3", _batch(), target) == target
    [command] = calls
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "atrim=start=10.000:end=11.000" in filter_graph
    assert "anullsrc=r=16000:cl=mono:d=0.500" in filter_graph
    assert "atrim=start=30.000:end=31.000" in filter_graph
    assert filter_graph.index("start=10.000") < filter_graph.index("anullsrc") < filter_graph.index("start=30.000")
    assert "20.000" not in filter_graph


def test_worker_uses_frozen_single_pass_whisper_options(monkeypatch) -> None:
    captured = {}

    def transcribe(path, **kwargs):
        captured.update(path=path, **kwargs)
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "你好"}], "language": "zh", "language_probability": 0.97}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))

    result = _transcribe_worker("batch.wav", "local-model", language="zh")

    assert captured == {
        "path": "batch.wav",
        "path_or_hf_repo": "local-model",
        "word_timestamps": False,
        "condition_on_previous_text": False,
        "temperature": 0,
        "language": "zh",
    }
    assert result.language == "zh"
    assert result.language_confidence == 0.97
    assert result[0]["text"] == "你好"


@pytest.mark.asyncio
async def test_development_transcription_rejects_nested_chunk_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, boundary, database, job_file = (
        await _development_transcription_fixture(tmp_path)
    )
    protected = tmp_path / "production-staging"
    protected.mkdir()
    chunk_dir = Path(job_file.temporary_path).with_name(
        f"{job_file.id}.whisper-chunks"
    )
    chunk_dir.symlink_to(protected, target_is_directory=True)

    class Vad:
        last_energy_intervals = []

        def detect(self, _path):
            return [SpeechInterval(0, 1_000)]

    def prepare(_source, _batch, target, **_kwargs):
        target.write_bytes(b"unsafe output")
        return target

    def worker(*_args):
        return WhisperBatchResult([], language="zh", language_confidence=1.0)

    monkeypatch.setattr(
        "audio_memory.transcription.engine.prepare_compact_wav", prepare
    )
    monkeypatch.setattr(
        "audio_memory.transcription.engine._transcribe_worker", worker
    )
    engine = MLXWhisperEngine(
        database,
        config.paths,
        runtime_profile=AppProfile.DEVELOPMENT,
        voice_activity_detector=Vad(),
        speech_padding_ms=0,
        write_boundary=boundary,
    )
    engine._executor = ThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(UnsafeDevelopmentPathError):
            async for _ in engine.transcribe_file(job_file, 0):
                pass
        assert not any(protected.iterdir())
    finally:
        await engine.close()
        await database.dispose()
        boundary.close()


@pytest.mark.asyncio
async def test_development_transcription_root_swap_cannot_write_or_delete_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, boundary, database, job_file = (
        await _development_transcription_fixture(tmp_path)
    )
    data_root = config.paths.root
    parked_root = data_root.with_name("dev-before-transcription-swap")
    protected = tmp_path / "production-target"
    protected_chunk_dir = (
        protected / "staging/job-1/file-1.whisper-chunks"
    )
    protected_chunk_dir.mkdir(parents=True)
    sentinel = protected_chunk_dir / "preserve.txt"
    sentinel.write_bytes(b"preserve exactly")
    swapped = False

    class Vad:
        last_energy_intervals = []

        def detect(self, _path):
            return [SpeechInterval(0, 1_000)]

    def prepare(
        _source,
        _batch,
        target,
        *,
        source_fd=None,
        target_fd=None,
    ):
        nonlocal swapped
        if not swapped:
            data_root.rename(parked_root)
            data_root.symlink_to(protected, target_is_directory=True)
            swapped = True
        if target_fd is None:
            target.write_bytes(b"unsafe output")
        else:
            os.write(target_fd, b"safe output")
        return target

    def worker(*_args):
        return WhisperBatchResult([], language="zh", language_confidence=1.0)

    monkeypatch.setattr(
        "audio_memory.transcription.engine.prepare_compact_wav", prepare
    )
    monkeypatch.setattr(
        "audio_memory.transcription.engine._transcribe_worker", worker
    )
    engine = MLXWhisperEngine(
        database,
        config.paths,
        runtime_profile=AppProfile.DEVELOPMENT,
        voice_activity_detector=Vad(),
        speech_padding_ms=0,
        write_boundary=boundary,
    )
    engine._executor = ThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(UnsafeDevelopmentPathError):
            async for _ in engine.transcribe_file(job_file, 0):
                pass
        assert swapped is True
        assert sentinel.read_bytes() == b"preserve exactly"
        assert list(protected_chunk_dir.iterdir()) == [sentinel]
    finally:
        await engine.close()
        await database.dispose()
        boundary.close()
        if data_root.is_symlink():
            data_root.unlink()
            parked_root.rename(data_root)


@pytest.mark.asyncio
async def test_development_transcription_uses_real_ffmpeg_with_anchored_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, boundary, database, job_file = (
        await _development_transcription_fixture(tmp_path)
    )
    encoded = BytesIO()
    with wave.open(encoded, "wb") as source_audio:
        source_audio.setnchannels(1)
        source_audio.setsampwidth(2)
        source_audio.setframerate(16_000)
        source_audio.writeframes(b"\0\0" * 16_000)
    boundary.write_bytes_atomic(
        Path(job_file.temporary_path), encoded.getvalue()
    )

    class Vad:
        last_energy_intervals = []

        def detect(self, _path):
            return [SpeechInterval(0, 1_000)]

    def worker(audio, *_args):
        assert getattr(audio, "shape", None) == (16_000,)
        return WhisperBatchResult([], language="zh", language_confidence=1.0)

    monkeypatch.setattr(
        "audio_memory.transcription.engine._transcribe_worker", worker
    )
    engine = MLXWhisperEngine(
        database,
        config.paths,
        runtime_profile=AppProfile.DEVELOPMENT,
        voice_activity_detector=Vad(),
        speech_padding_ms=0,
        write_boundary=boundary,
    )
    engine._executor = ThreadPoolExecutor(max_workers=1)
    try:
        assert [item async for item in engine.transcribe_file(job_file, 0)] == []
        assert engine.metrics_by_job[job_file.job_id]["counts"] == {
            "whisper_calls": 1
        }
        assert not Path(job_file.temporary_path).with_name(
            f"{job_file.id}.whisper-chunks"
        ).exists()
    finally:
        await engine.close()
        await database.dispose()
        boundary.close()


@pytest.mark.asyncio
async def test_checkpoint_skips_completed_batch_and_restores_language_lock(
    tmp_path: Path, monkeypatch
) -> None:
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"synthetic")
    file = JobFile(
        id="file-1", job_id="job-1", original_name="source.mp3", extension=".mp3",
        size_bytes=9, sha256="a" * 64, duration_ms=1_800_000, position=0,
        temporary_path=str(source),
    )
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(file)
        await session.commit()

    class Vad:
        last_energy_intervals = []

        def detect(self, _path):
            return [SpeechInterval(0, 1_800_000)]

    def prepare(_source, _batch, target):
        target.write_bytes(b"wav")
        return target

    calls: list[tuple[int, str | None]] = []
    fail_second = True

    def worker(path, _model, _words=False, language=None):
        nonlocal fail_second
        index = int(Path(path).stem.rsplit("-", 1)[1])
        calls.append((index, language))
        if index == 1 and fail_second:
            fail_second = False
            raise RuntimeError("interrupted")
        return WhisperBatchResult(
            [{"start": 10.0, "end": 11.0, "text": f"batch {index}"}],
            language="zh",
            language_confidence=0.95,
        )

    monkeypatch.setattr("audio_memory.transcription.engine.prepare_compact_wav", prepare)
    monkeypatch.setattr("audio_memory.transcription.engine._transcribe_worker", worker)
    engine = MLXWhisperEngine(database, paths, voice_activity_detector=Vad(), speech_padding_ms=0)
    engine._executor = ThreadPoolExecutor(max_workers=1)
    generator = engine.transcribe_file(file, 0)
    first = await anext(generator)
    assert first.speaker_id == "unknown"
    with pytest.raises(RuntimeError, match="interrupted"):
        await anext(generator)
    assert '"last_completed_batch":0' in file.compact_checkpoint_json

    calls.clear()
    resumed = [item async for item in engine.transcribe_file(file, 0)]
    assert [item.text for item in resumed] == ["batch 1"]
    assert calls == [(1, "zh")]
    await engine.close()
    await database.dispose()
