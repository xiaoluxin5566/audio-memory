from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import sys

import pytest

from audio_memory.config import AppPaths
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
