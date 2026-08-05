from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from audio_memory.api.jobs import router
from audio_memory.config import AppPaths
from audio_memory.db import Database
from audio_memory.diarization.engine import SpeakerTurn
from audio_memory.models import AnalysisJob, JobFile, Transcript
from audio_memory.uploads.service import UploadService
from audio_memory.transcription.engine import (
    MLXWhisperEngine,
    SelectiveRefiner,
    SpeechInterval,
    _transcribe_worker,
    diarize_fail_open,
    valid_chunk_segments,
)
from audio_memory.transcription.checkpoints import TranscriptionService


def test_installed_sherpa_runtime_imports() -> None:
    import sherpa_onnx

    assert hasattr(sherpa_onnx, "OfflineSpeakerDiarization")


def make_timestamped_mp3(path: Path, creation_time: str) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.1",
            "-metadata",
            f"creation_time={creation_time}",
            "-c:a",
            "libmp3lame",
            "-y",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def make_plain_mp3(path: Path) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.1",
            "-c:a",
            "libmp3lame",
            "-y",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@pytest.mark.asyncio
async def test_real_ffmpeg_extracts_only_requested_speech_intervals(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp3"
    make_plain_mp3(source)
    target_dir = tmp_path / "speech"

    clips = await MLXWhisperEngine._extract_speech_intervals(
        source,
        target_dir,
        [SpeechInterval(20, 60)],
    )

    assert [item.name for item in clips] == ["speech-00000.wav"]
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(clips[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert 0.02 <= float(probe.stdout.strip()) <= 0.08


@pytest.mark.asyncio
async def test_embedded_creation_time_overrides_browser_modified_time(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    app = FastAPI()
    app.state.upload_service = UploadService(database, paths)
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    content = make_timestamped_mp3(
        tmp_path / "timestamped.mp3", "2025-01-02T03:04:05+08:00"
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = (await client.post("/api/jobs")).json()["id"]
        response = await client.post(
            f"/api/jobs/{job_id}/files",
            data={
                "file_modified": "1893456000000",
                "timezone": "Asia/Shanghai",
            },
            files={"file": ("timestamped.mp3", content, "audio/mpeg")},
        )

    assert response.status_code == 201
    assert response.json()["recording_started_at"] == "2025-01-01T19:04:05+00:00"
    assert response.json()["recording_time_source"] == "embedded"
    assert response.json()["timezone"] == "Asia/Shanghai"
    async with database.session() as session:
        stored = await session.get(JobFile, response.json()["id"])
    assert stored.recording_started_at == "2025-01-01T19:04:05+00:00"
    assert stored.recording_time_source == "embedded"
    await database.dispose()


@pytest.mark.asyncio
async def test_browser_modified_time_is_used_when_embedded_time_is_absent(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    app = FastAPI()
    app.state.upload_service = UploadService(database, paths)
    app.include_router(router)
    content = make_plain_mp3(tmp_path / "plain.mp3")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        job_id = (await client.post("/api/jobs")).json()["id"]
        response = await client.post(
            f"/api/jobs/{job_id}/files",
            data={"file_modified": "1000", "timezone": "Asia/Shanghai"},
            files={"file": ("plain.mp3", content, "audio/mpeg")},
        )

    assert response.status_code == 201
    assert response.json()["recording_started_at"] == "1970-01-01T00:00:01+00:00"
    assert response.json()["recording_time_source"] == "file_modified"
    await database.dispose()


@pytest.mark.asyncio
async def test_missing_recording_time_stays_unknown_instead_of_using_upload_time(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    app = FastAPI()
    app.state.upload_service = UploadService(database, paths)
    app.include_router(router)
    content = make_plain_mp3(tmp_path / "unknown.mp3")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        job_id = (await client.post("/api/jobs")).json()["id"]
        response = await client.post(
            f"/api/jobs/{job_id}/files",
            files={"file": ("unknown.mp3", content, "audio/mpeg")},
        )

    assert response.json()["recording_started_at"] is None
    assert response.json()["recording_time_source"] == "unknown"
    async with database.session() as session:
        stored = await session.get(JobFile, response.json()["id"])
    assert stored.recording_time_source == "unknown"
    await database.dispose()


def test_default_whisper_worker_uses_segment_timestamps(monkeypatch) -> None:
    def transcribe(_path, *, path_or_hf_repo, word_timestamps=False):
        segment = {"start": 0.0, "end": 1.0, "text": "你好"}
        if word_timestamps:
            segment["words"] = [{"word": "你好", "start": 0.0, "end": 1.0}]
        return {"segments": [segment], "language": "zh"}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))

    segments = _transcribe_worker("local.mp3", "local-model")

    assert "words" not in segments[0]


@pytest.mark.asyncio
async def test_selective_refiner_alone_requests_word_timestamps(
    tmp_path: Path, monkeypatch
) -> None:
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"local audio")
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=11,
                sha256="a" * 64,
                duration_ms=10_000,
                position=0,
                temporary_path=str(source),
            )
        )
        session.add(
            Transcript(
                id="transcript-1",
                job_file_id="file-1",
                segment_index=0,
                segment_uid="file-1:0",
                speaker_id="speaker_00",
                start_ms=2_000,
                end_ms=3_000,
                text="默认文字",
            )
        )
        await session.commit()

    calls: list[bool] = []

    def worker(_path, _model_id, word_timestamps=False):
        calls.append(word_timestamps)
        return [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "默认文字",
                "words": [
                    {"word": "默认", "start": 0.1, "end": 0.4},
                    {"word": "文字", "start": 0.4, "end": 0.9},
                ],
            }
        ]

    refiner = SelectiveRefiner(database, worker=worker)

    async def extract(_source, target, _start_ms, _end_ms):
        target.write_bytes(b"pcm")

    monkeypatch.setattr(refiner, "_extract_segment", extract)

    refined = await refiner.refine(["file-1:0"])

    assert calls == [True]
    assert refined[0].text == "默认文字"
    assert [(item.start_ms, item.end_ms) for item in refined[0].words] == [
        (2_100, 2_400),
        (2_400, 2_900),
    ]
    assert refined[0].speaker_id == "speaker_00"
    await database.dispose()


@pytest.mark.asyncio
async def test_selective_refinement_failure_preserves_default_segment(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"local audio")
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=11,
                sha256="b" * 64,
                duration_ms=10_000,
                position=0,
                temporary_path=str(source),
            )
        )
        session.add(
            Transcript(
                id="transcript-1",
                job_file_id="file-1",
                segment_index=0,
                segment_uid="file-1:0",
                start_ms=2_000,
                end_ms=3_000,
                text="不能丢的原文",
            )
        )
        await session.commit()

    def failing_worker(*_args, **_kwargs):
        raise RuntimeError("mlx failed")

    refiner = SelectiveRefiner(database, worker=failing_worker)

    async def extract(_source, target, _start_ms, _end_ms):
        target.write_bytes(b"pcm")

    monkeypatch.setattr(refiner, "_extract_segment", extract)

    refined = await refiner.refine(["file-1:0"])

    assert [(item.start_ms, item.end_ms, item.text, item.words) for item in refined] == [
        (2_000, 3_000, "不能丢的原文", ()),
    ]
    assert "diagnostic=selective_refinement_failed" in caplog.text
    await database.dispose()


def test_diarization_failure_preserves_unlabeled_transcript(caplog) -> None:
    class FailingDiarizer:
        def diarize(self, _path):
            raise RuntimeError("model unavailable")

    turns = diarize_fail_open(FailingDiarizer(), Path("local.mp3"))
    segments = list(
        valid_chunk_segments(
            file_id="file-1",
            chunk_index=0,
            chunk_seconds=300,
            raw_segments=[
                {"start": 0.0, "end": 1.0, "text": "保留完整文字", "words": []}
            ],
            turns=turns,
        )
    )

    assert [(item.text, item.speaker_id) for item in segments] == [
        ("保留完整文字", None)
    ]
    assert "diagnostic=diarization_failed" in caplog.text


@pytest.mark.asyncio
async def test_five_hour_sparse_audio_only_transcribes_padded_speech(
    tmp_path: Path, monkeypatch
) -> None:
    transcribed_paths: list[str] = []

    def transcribe(path, *, path_or_hf_repo, word_timestamps=False):
        assert word_timestamps is False
        transcribed_paths.append(path)
        return {"segments": [{"start": 0.0, "end": 0.5, "text": "语音"}]}

    class SparseVad:
        def detect(self, _path):
            return [
                SpeechInterval(60_000, 62_000),
                SpeechInterval(17_999_000, 18_000_000),
            ]

    diarized_paths: list[Path] = []

    class RecordingDiarizer:
        def diarize(self, path):
            diarized_paths.append(Path(path))
            return [SpeakerTurn(0, 500, "local_00")]

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"sparse local audio")
    file = JobFile(
        id="file-1",
        job_id="job-1",
        original_name="source.mp3",
        extension=".mp3",
        size_bytes=18,
        sha256="a" * 64,
        duration_ms=18_000_000,
        position=0,
        temporary_path=str(source),
    )
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(file)
        await session.commit()
    engine = MLXWhisperEngine(
        database,
        paths,
        voice_activity_detector=SparseVad(),
        diarization_engine=RecordingDiarizer(),
        speech_padding_ms=250,
    )
    engine._executor = ThreadPoolExecutor(max_workers=1)
    extracted: list[SpeechInterval] = []

    async def extract_speech(_source, target_dir, intervals):
        target_dir.mkdir()
        extracted.extend(intervals)
        clips = []
        for index, _interval in enumerate(intervals):
            clip = target_dir / f"speech-{index:05d}.wav"
            clip.write_bytes(b"pcm")
            clips.append(clip)
        return clips

    monkeypatch.setattr(engine, "_extract_speech_intervals", extract_speech)

    segments = [item async for item in engine.transcribe_file(file, 0)]

    assert [item.text for item in segments] == ["语音", "语音"]
    assert [item.speaker_id for item in segments] == ["speaker_00", "speaker_01"]
    assert extracted == [
        SpeechInterval(59_750, 62_250),
        SpeechInterval(17_998_750, 18_000_000),
    ]
    assert sum(item.end_ms - item.start_ms for item in extracted) == 3_750
    assert [Path(item) for item in transcribed_paths] == diarized_paths
    mapping = json.loads(file.speech_mapping_json)
    assert mapping[-1]["compact_end_ms"] == 3_750
    async with database.session() as session:
        stored = await session.get(JobFile, "file-1")
    assert stored is not None
    assert stored.speech_mapping_json == file.speech_mapping_json
    await engine.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_overlapping_windows_coordinate_speakers_and_deduplicate_whisper(
    tmp_path: Path, monkeypatch
) -> None:
    def transcribe(path, *, path_or_hf_repo, word_timestamps=False):
        index = int(Path(path).stem.split("-")[-1])
        if index == 0:
            return {
                    "segments": [
                        {"start": 10.0, "end": 11.0, "text": "首段"},
                        {"start": 1798.0, "end": 1800.5, "text": "重叠"},
                ]
            }
        return {
            "segments": [
                {"start": 29.5, "end": 31.5, "text": "重叠"},
                {"start": 35.0, "end": 36.0, "text": "尾段"},
            ]
        }

    class LongSpeechVad:
        def detect(self, _path):
            return [SpeechInterval(0, 1_810_000)]

    class WindowDiarizer:
        calls = 0

        def diarize(self, _path):
            self.calls += 1
            if self.calls == 1:
                return [SpeakerTurn(1_797_000, 1_800_000, "local_00")]
            return [SpeakerTurn(27_000, 31_000, "local_00")]

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"long local audio")
    file = JobFile(
        id="file-1",
        job_id="job-1",
        original_name="source.mp3",
        extension=".mp3",
        size_bytes=16,
        sha256="c" * 64,
        duration_ms=1_810_000,
        position=0,
        temporary_path=str(source),
    )
    engine = MLXWhisperEngine(
        database,
        paths,
        voice_activity_detector=LongSpeechVad(),
        diarization_engine=WindowDiarizer(),
        speech_padding_ms=0,
    )
    engine._executor = ThreadPoolExecutor(max_workers=1)
    extracted: list[SpeechInterval] = []

    async def extract_speech(_source, target_dir, intervals):
        target_dir.mkdir()
        extracted.extend(intervals)
        clips = []
        for index, _interval in enumerate(intervals):
            clip = target_dir / f"speech-{index:05d}.wav"
            clip.write_bytes(b"pcm")
            clips.append(clip)
        return clips

    monkeypatch.setattr(engine, "_extract_speech_intervals", extract_speech)

    segments = [item async for item in engine.transcribe_file(file, 0)]

    assert extracted == [
        SpeechInterval(0, 1_800_000),
        SpeechInterval(1_770_000, 1_810_000),
    ]
    assert [item.text for item in segments] == ["首段", "重叠", "尾段"]
    overlap = next(item for item in segments if item.text == "重叠")
    assert overlap.speaker_id == "speaker_00"
    assert json.loads(file.speech_mapping_json)[-1]["compact_end_ms"] == 1_810_000
    await engine.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_checkpoint_resume_after_partial_second_speech_interval(
    tmp_path: Path, monkeypatch
) -> None:
    def transcribe(path, *, path_or_hf_repo, word_timestamps=False):
        index = int(Path(path).stem.split("-")[-1])
        return {
            "segments": [
                {"start": start, "end": end, "text": text}
                for start, end, text in (
                    [(0.1, 0.2, "A")]
                    if index == 0
                    else [(0.1, 0.2, "B1"), (0.3, 0.4, "B2")]
                    if index == 1
                    else [(0.1, 0.2, "C")]
                )
            ]
        }

    class ThreeIntervalVad:
        def detect(self, _path):
            return [
                SpeechInterval(10_000, 11_000),
                SpeechInterval(20_000, 22_000),
                SpeechInterval(30_000, 31_000),
            ]

    class LocalDiarizer:
        def diarize(self, _path):
            return [SpeakerTurn(0, 500, "local_00")]

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"checkpoint audio")
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=16,
                sha256="d" * 64,
                duration_ms=40_000,
                position=0,
                temporary_path=str(source),
            )
        )
        await session.commit()

    engine = MLXWhisperEngine(
        database,
        paths,
        voice_activity_detector=ThreeIntervalVad(),
        diarization_engine=LocalDiarizer(),
        speech_padding_ms=0,
    )
    engine._executor = ThreadPoolExecutor(max_workers=1)

    async def extract_speech(_source, target_dir, intervals):
        target_dir.mkdir()
        clips = []
        for index, _interval in enumerate(intervals):
            clip = target_dir / f"speech-{index:05d}.wav"
            clip.write_bytes(b"pcm")
            clips.append(clip)
        return clips

    monkeypatch.setattr(engine, "_extract_speech_intervals", extract_speech)

    class InterruptDuringSecondInterval:
        async def transcribe_file(self, file, resume_from):
            stream = engine.transcribe_file(file, resume_from)
            emitted = 0
            try:
                async for segment in stream:
                    yield segment
                    emitted += 1
                    if emitted == 2:
                        raise RuntimeError("simulated interruption")
            finally:
                await stream.aclose()

    service = TranscriptionService(database)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await service.run_job("job-1", InterruptDuringSecondInterval())

    await service.resume_job("job-1", engine)

    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )
    assert [item.segment_uid for item in rows] == [
        "file-1:0",
        "file-1:10000",
        "file-1:10001",
        "file-1:20000",
    ]
    assert len({item.segment_uid for item in rows}) == 4
    assert [
        (item.start_ms, item.end_ms, item.text) for item in rows
    ] == [
        (10_100, 10_200, "A"),
        (20_100, 20_200, "B1"),
        (20_300, 20_400, "B2"),
        (30_100, 30_200, "C"),
    ]
    assert [item.speaker_id for item in rows] == [
        "speaker_00",
        "speaker_01",
        "speaker_01",
        "speaker_02",
    ]
    await engine.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_whisper_pipeline_continues_when_vad_fails(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    def transcribe(_path, *, path_or_hf_repo, word_timestamps=False):
        return {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "这段文字不能丢",
                    "words": [{"word": "这段文字不能丢", "start": 0.0, "end": 1.0}],
                }
            ]
        }

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"local audio")
    file = JobFile(
        id="file-1",
        job_id="job-1",
        original_name="source.mp3",
        extension=".mp3",
        size_bytes=11,
        sha256="a" * 64,
        duration_ms=1000,
        position=0,
        temporary_path=str(source),
    )
    class FailingVad:
        def detect(self, _path):
            raise RuntimeError("vad unavailable")

    class UnexpectedDiarizer:
        def diarize(self, _path):
            raise AssertionError("fallback chunks are not confirmed speech intervals")

    engine = MLXWhisperEngine(
        database,
        paths,
        voice_activity_detector=FailingVad(),
        diarization_engine=UnexpectedDiarizer(),
    )
    engine._executor = ThreadPoolExecutor(max_workers=1)

    async def make_one_chunk(_source: Path, target_dir: Path) -> list[Path]:
        target_dir.mkdir()
        chunk = target_dir / "chunk-00000.wav"
        chunk.write_bytes(b"local pcm")
        return [chunk]

    monkeypatch.setattr(engine, "_normalize_to_chunks", make_one_chunk)

    segments = [item async for item in engine.transcribe_file(file, 0)]

    assert [(item.text, item.speaker_id) for item in segments] == [
        ("这段文字不能丢", None)
    ]
    assert "diagnostic=vad_failed" in caplog.text
    await engine.close()
    await database.dispose()
