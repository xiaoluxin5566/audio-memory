from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from audio_memory.api.jobs import router
from audio_memory.config import AppPaths
from audio_memory.db import Database
from audio_memory.diarization.engine import SpeakerTurn
from audio_memory.diarization.alignment import AlignedTranscriptSegment, Word
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
from audio_memory.transcription.risk_service import TranscriptionRiskGateService
from audio_memory.transcription.risk_gate import EnergyInterval


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
                text="",
                risk_state="HIGH_RISK_PENDING",
                is_reliable=False,
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
                "text": "精转写结果",
                "words": [
                    {"word": "精转写", "start": 0.1, "end": 0.4},
                    {"word": "结果", "start": 0.4, "end": 0.9},
                ],
            }
        ]

    refiner = SelectiveRefiner(database, worker=worker)

    async def extract(_source, target, _start_ms, _end_ms):
        target.write_bytes(b"pcm")

    monkeypatch.setattr(refiner, "_extract_segment", extract)

    refined = await refiner.refine(["file-1:0"])

    assert calls == [True]
    assert refined[0].text == "精转写结果"
    assert [(item.start_ms, item.end_ms) for item in refined[0].words] == [
        (2_100, 2_400),
        (2_400, 2_900),
    ]
    assert refined[0].speaker_id == "speaker_00"
    await database.dispose()


def test_selective_refiner_preserves_english_word_boundaries_across_raw_segments() -> None:
    # Replacing the safe joining rule with direct concatenation would turn this
    # into the corrupt text "helloworldagain".
    assert SelectiveRefiner._source_text(
        [
            {"text": "hello"},
            {"text": " world"},
        ],
        [],
    ) == "hello world"
    assert SelectiveRefiner._source_text(
        [],
        [
            Word("hello", 0, 100),
            Word("world", 100, 200),
        ],
    ) == "hello world"


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
        (2_000, 3_000, "", ()),
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
        last_energy_intervals = [
            EnergyInterval(0, 1_000, has_signal=True),
            EnergyInterval(1_000, 2_000, has_signal=False),
        ]

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
    assert stored.vad_available is True
    assert json.loads(stored.vad_speech_json) == [
        {"start_ms": 60_000, "end_ms": 62_000},
        {"start_ms": 17_999_000, "end_ms": 18_000_000},
    ]
    assert json.loads(stored.vad_energy_json) == [
        {"start_ms": 0, "end_ms": 1_000, "has_signal": True},
        {"start_ms": 1_000, "end_ms": 2_000, "has_signal": False},
    ]
    await engine.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_file_classification_commit_rolls_back_as_one_recoverable_snapshot(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "risk-gate-atomic-snapshot.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        file = JobFile(
            id="file-1",
            job_id="job-1",
            original_name="source.mp3",
            extension=".mp3",
            size_bytes=10,
            sha256="q" * 64,
            duration_ms=10_000,
            speech_mapping_json=(
                '[{"compact_start_ms":0,"compact_end_ms":10000,'
                '"source_start_ms":0,"source_end_ms":10000}]'
            ),
            position=0,
            temporary_path=str(tmp_path / "source.mp3"),
        )
        file.vad_speech_json = '[{"start_ms":0,"end_ms":10000}]'
        session.add(file)
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="three identical fast transcripts",
                    words_json="[]",
                )
                for index in range(3)
            ]
        )
        await session.commit()

    commits = 0

    def interrupt_during_classification(session: Session) -> None:
        nonlocal commits
        dirty_transcripts = [
            item for item in session.dirty if isinstance(item, Transcript)
        ]
        if len(dirty_transcripts) >= 3 or commits == 2:
            raise RuntimeError("injected classification persistence failure")
        if dirty_transcripts:
            commits += 1

    event.listen(Session, "before_commit", interrupt_during_classification)
    gate = TranscriptionRiskGateService(database)
    try:
        with pytest.raises(
            RuntimeError, match="injected classification persistence failure"
        ):
            await gate.apply("job-1", object())
    finally:
        event.remove(Session, "before_commit", interrupt_during_classification)

    async with database.session() as session:
        after_failure = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )
    assert [item.risk_classified for item in after_failure] == [False, False, False]
    assert [item.text for item in after_failure] == [
        "three identical fast transcripts",
        "three identical fast transcripts",
        "three identical fast transcripts",
    ]

    class SafeRefiner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def refine(self, segment_uids: list[str]):
            self.calls.extend(segment_uids)
            return [
                AlignedTranscriptSegment(
                    start_ms=4_000,
                    end_ms=5_000,
                    text="recovered accurate transcript",
                    words=(Word("recovered", 4_000, 5_000),),
                    speaker_id=None,
                )
            ]

    refiner = SafeRefiner()
    await gate.apply("job-1", refiner)

    async with database.session() as session:
        recovered = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )
    assert refiner.calls == ["file-1:2"]
    assert recovered[2].risk_state == "POST_EDIT_PASSED"
    assert recovered[2].text == "recovered accurate transcript"
    await database.dispose()


@pytest.mark.asyncio
async def test_service_hard_rejects_file_overflow_corruption_and_time_conflicts(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "risk-gate-file-boundaries.sqlite3")
    await database.create_schema()
    rows = [
        ("head", -100, 500, "head overflow"),
        ("tail", 800, 1_100, "tail overflow"),
        ("timing", 500, 500, "invalid timing"),
        ("blank", 0, 500, "   "),
    ]
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        for position, (name, start_ms, end_ms, text) in enumerate(rows):
            file = JobFile(
                id=f"file-{name}",
                job_id="job-1",
                original_name=f"{name}.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256=f"{position + 1:064x}",
                duration_ms=1_000,
                speech_mapping_json=(
                    '[{"compact_start_ms":0,"compact_end_ms":1000,'
                    '"source_start_ms":0,"source_end_ms":1000}]'
                ),
                position=position,
                temporary_path=str(tmp_path / f"{name}.mp3"),
            )
            file.vad_speech_json = '[{"start_ms":0,"end_ms":1000}]'
            session.add(file)
            session.add(
                Transcript(
                    id=f"transcript-{name}",
                    job_file_id=file.id,
                    segment_index=0,
                    segment_uid=f"{file.id}:0",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                    words_json="[]",
                )
            )
        conflict_file = JobFile(
            id="file-conflict",
            job_id="job-1",
            original_name="conflict.mp3",
            extension=".mp3",
            size_bytes=10,
            sha256="f" * 64,
            duration_ms=3_000,
            speech_mapping_json=(
                '[{"compact_start_ms":0,"compact_end_ms":3000,'
                '"source_start_ms":0,"source_end_ms":3000}]'
            ),
            position=len(rows),
            temporary_path=str(tmp_path / "conflict.mp3"),
        )
        conflict_file.vad_speech_json = '[{"start_ms":0,"end_ms":3000}]'
        session.add(conflict_file)
        session.add_all(
            [
                Transcript(
                    id=f"conflict-{index}",
                    job_file_id=conflict_file.id,
                    segment_index=index,
                    segment_uid=f"file-conflict:{index}",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=f"conflict {index}",
                    words_json="[]",
                )
                for index, (start_ms, end_ms) in enumerate(
                    [(0, 1_500), (1_000, 2_000)]
                )
            ]
        )
        await session.commit()

    class UnexpectedRefiner:
        async def refine(self, _segment_uids: list[str]):
            raise AssertionError("hard-rejected rows must not be refined")

    metrics = await TranscriptionRiskGateService(database).apply(
        "job-1", UnexpectedRefiner()
    )
    async with database.session() as session:
        stored = list(await session.scalars(select(Transcript)))
    reasons = {item.id: item.risk_reason for item in stored}
    assert reasons == {
        "transcript-head": "outside_file_window",
        "transcript-tail": "outside_file_window",
        "transcript-timing": "invalid_timing",
        "transcript-blank": "blank_text",
        "conflict-0": "timestamp_conflict",
        "conflict-1": "timestamp_conflict",
    }
    assert metrics.rejected == 6
    assert all(item.risk_state == "REJECTED" for item in stored)
    assert all(item.text == "" for item in stored)
    await database.dispose()


@pytest.mark.asyncio
async def test_risk_gate_uses_raw_vad_for_rate_not_the_padded_processing_mapping(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "risk-gate-raw-vad.sqlite3")
    await database.create_schema()
    repeated = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        file = JobFile(
            id="file-1",
            job_id="job-1",
            original_name="source.mp3",
            extension=".mp3",
            size_bytes=10,
            sha256="r" * 64,
            duration_ms=5_000,
            speech_mapping_json=(
                '[{"compact_start_ms":0,"compact_end_ms":5000,'
                '"source_start_ms":0,"source_end_ms":5000}]'
            ),
            position=0,
            temporary_path=str(tmp_path / "source.mp3"),
        )
        file.vad_speech_json = (
            '[{"start_ms":0,"end_ms":2000},'
            '{"start_ms":3000,"end_ms":3600}]'
        )
        session.add(file)
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=repeated,
                    words_json="[]",
                )
                for index, (start_ms, end_ms) in enumerate([(0, 2_000), (3_000, 5_000)])
            ]
        )
        await session.commit()

    class RecordingRefiner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def refine(self, segment_uids: list[str]):
            self.calls.extend(segment_uids)
            return [
                AlignedTranscriptSegment(
                    start_ms=3_000,
                    end_ms=5_000,
                    text="safe replacement",
                    words=(Word("safe", 3_000, 5_000),),
                    speaker_id=None,
                )
            ]

    refiner = RecordingRefiner()
    await TranscriptionRiskGateService(database).apply("job-1", refiner)

    assert refiner.calls == ["file-1:1"]
    await database.dispose()


@pytest.mark.asyncio
async def test_refinement_recheck_counts_replacement_and_excludes_target_old_text(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "risk-gate-replacement-context.sqlite3")
    await database.create_schema()
    nearby_text = "new nearby result"
    old_repeated = "old phrase old phrase old phrase"
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        file = JobFile(
            id="file-1",
            job_id="job-1",
            original_name="source.mp3",
            extension=".mp3",
            size_bytes=10,
            sha256="s" * 64,
            duration_ms=12_000,
            speech_mapping_json=(
                '[{"compact_start_ms":0,"compact_end_ms":12000,'
                '"source_start_ms":0,"source_end_ms":12000}]'
            ),
            position=0,
            temporary_path=str(tmp_path / "source.mp3"),
        )
        file.vad_speech_json = '[{"start_ms":0,"end_ms":12000}]'
        session.add(file)
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 4_000,
                    end_ms=index * 4_000 + 1_000,
                    text=text,
                    words_json="[]",
                )
                for index, text in enumerate([nearby_text, nearby_text, old_repeated])
            ]
        )
        await session.commit()

    class ReplacingRefiner:
        async def refine(self, segment_uids: list[str]):
            assert segment_uids == ["file-1:2"]
            return [
                AlignedTranscriptSegment(
                    start_ms=8_000,
                    end_ms=9_000,
                    text=nearby_text,
                    words=(Word("nearby", 8_000, 9_000),),
                    speaker_id=None,
                )
            ]

    await TranscriptionRiskGateService(database).apply("job-1", ReplacingRefiner())
    async with database.session() as session:
        target = await session.get(Transcript, "transcript-2")
    assert target is not None
    assert (target.risk_state, target.is_reliable, target.text) == (
        "POST_EDIT_FAILED",
        False,
        "",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_classification_time_exhausts_total_budget_before_any_refinement(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(tmp_path / "risk-gate-total-budget.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        file = JobFile(
            id="file-1",
            job_id="job-1",
            original_name="source.mp3",
            extension=".mp3",
            size_bytes=10,
            sha256="t" * 64,
            duration_ms=10_000,
            speech_mapping_json=(
                '[{"compact_start_ms":0,"compact_end_ms":10000,'
                '"source_start_ms":0,"source_end_ms":10000}]'
            ),
            position=0,
            temporary_path=str(tmp_path / "source.mp3"),
        )
        file.vad_speech_json = '[{"start_ms":0,"end_ms":10000}]'
        session.add(file)
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="budget repeated transcript",
                    words_json="[]",
                )
                for index in range(3)
            ]
        )
        await session.commit()

    from audio_memory.transcription import risk_service

    real_classifier = risk_service.classify_segments

    def delayed_classifier(*args, **kwargs):
        time.sleep(0.02)
        return real_classifier(*args, **kwargs)

    monkeypatch.setattr(risk_service, "classify_segments", delayed_classifier)

    class RecordingRefiner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def refine(self, segment_uids: list[str]):
            self.calls.extend(segment_uids)
            return []

    refiner = RecordingRefiner()
    metrics = await TranscriptionRiskGateService(database).apply(
        "job-1", refiner, bulk_elapsed_seconds=0.01
    )
    async with database.session() as session:
        target = await session.get(Transcript, "transcript-2")
    assert refiner.calls == []
    assert metrics.queued == 0
    assert metrics.overflowed == 1
    assert target is not None
    assert (target.risk_state, target.is_reliable, target.reliability_weight) == (
        None,
        True,
        0.6,
    )
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
                    {"start": 1784.5, "end": 1786.5, "text": "重叠句子"},
                ]
            }
        return {
            "segments": [
                {"start": 14.0, "end": 15.0, "text": "重叠语句"},
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
                return [SpeakerTurn(1_783_000, 1_787_000, "local_00")]
            return [SpeakerTurn(13_000, 17_000, "local_00")]

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
    assert [item.text for item in segments] == ["首段", "重叠句子", "尾段"]
    overlap = next(item for item in segments if item.text == "重叠句子")
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

    service = TranscriptionService(
        database,
        risk_gate=TranscriptionRiskGateService(database),
        refiner=SelectiveRefiner(database),
    )
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
async def test_checkpoint_resume_preserves_reverse_order_boundary_segments(
    tmp_path: Path, monkeypatch
) -> None:
    def transcribe(path, *, path_or_hf_repo, word_timestamps=False):
        index = int(Path(path).stem.split("-")[-1])
        if index == 0:
            return {
                "segments": [
                    {
                        "start": 1784.8,
                        "end": 1786.8,
                        "text": "前窗后句记录完成",
                    }
                ]
            }
        return {
            "segments": [
                {"start": 14.0, "end": 15.0, "text": "后窗前句转入讨论"}
            ]
        }

    class LongSpeechVad:
        def detect(self, _path):
            return [SpeechInterval(0, 1_810_000)]

    class NoSpeakerDiarizer:
        def diarize(self, _path):
            return []

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    paths = AppPaths.from_home(tmp_path)
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source_dir = paths.staging / "job-1"
    source_dir.mkdir()
    source = source_dir / "source.mp3"
    source.write_bytes(b"reverse boundary checkpoint audio")
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=33,
                sha256="e" * 64,
                duration_ms=1_810_000,
                position=0,
                temporary_path=str(source),
            )
        )
        await session.commit()

    engine = MLXWhisperEngine(
        database,
        paths,
        voice_activity_detector=LongSpeechVad(),
        diarization_engine=NoSpeakerDiarizer(),
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

    class InterruptBetweenBoundarySegments:
        async def transcribe_file(self, file, resume_from):
            stream = engine.transcribe_file(file, resume_from)
            emitted = 0
            try:
                async for segment in stream:
                    yield segment
                    emitted += 1
                    if emitted == 1:
                        raise RuntimeError("simulated boundary interruption")
            finally:
                await stream.aclose()

    service = TranscriptionService(
        database,
        risk_gate=TranscriptionRiskGateService(database),
        refiner=SelectiveRefiner(database),
    )
    with pytest.raises(RuntimeError, match="simulated boundary interruption"):
        await service.run_job("job-1", InterruptBetweenBoundarySegments())

    await service.resume_job("job-1", engine)

    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )
    assert [item.segment_uid for item in rows] == ["file-1:0", "file-1:10000"]
    assert len({item.segment_uid for item in rows}) == 2
    assert [(item.start_ms, item.end_ms) for item in rows] == [
        (1_784_800, 1_786_800),
        (1_784_000, 1_785_000),
    ]
    assert all(
        item.risk_state == "REJECTED"
        and item.risk_reason == "timestamp_conflict"
        and item.text == ""
        for item in rows
    )

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
    assert file.vad_available is False
    assert file.vad_speech_json == "[]"
    assert "diagnostic=vad_failed" in caplog.text
    await engine.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_risk_gate_refines_one_repeated_segment_replaces_text_and_never_requeues(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "risk-gate.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="f" * 64,
                duration_ms=10_000,
                speech_mapping_json=(
                    '[{"compact_start_ms":0,"compact_end_ms":10000,'
                    '"source_start_ms":0,"source_end_ms":10000}]'
                ),
                vad_speech_json='[{"start_ms":0,"end_ms":10000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="重复的转写文本",
                    words_json="[]",
                )
                for index in range(3)
            ]
        )
        await session.commit()

    class SafeRefiner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def refine(self, segment_uids: list[str]):
            self.calls.extend(segment_uids)
            return [
                AlignedTranscriptSegment(
                    start_ms=4_000,
                    end_ms=5_000,
                    text="精转写后的不同文本",
                    words=(Word("精转写后的不同文本", 4_000, 5_000),),
                    speaker_id=None,
                )
            ]

    refiner = SafeRefiner()
    gate = TranscriptionRiskGateService(database)

    metrics = await gate.apply("job-1", refiner)
    await gate.apply("job-1", refiner)

    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )
    assert refiner.calls == ["file-1:2"]
    assert metrics.queued == 1
    assert (rows[1].is_reliable, rows[1].reliability_weight) == (True, 0.6)
    assert (rows[2].risk_state, rows[2].is_reliable, rows[2].text) == (
        "POST_EDIT_PASSED",
        True,
        "精转写后的不同文本",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_risk_gate_isolates_repeated_refinement_and_downgrades_queue_overflow(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "risk-gate-overflow.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="g" * 64,
                duration_ms=30_000,
                speech_mapping_json=(
                    '[{"compact_start_ms":0,"compact_end_ms":30000,'
                    '"source_start_ms":0,"source_end_ms":30000}]'
                ),
                vad_speech_json='[{"start_ms":0,"end_ms":30000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="仍然重复的转写文本",
                    words_json="[]",
                )
                for index in range(13)
            ]
        )
        await session.commit()

    class RepeatingRefiner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def refine(self, segment_uids: list[str]):
            self.calls.extend(segment_uids)
            return [
                AlignedTranscriptSegment(
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="仍然重复的转写文本",
                    words=(
                        Word(
                            "仍然重复的转写文本",
                            index * 2_000,
                            index * 2_000 + 1_000,
                        ),
                    ),
                    speaker_id=None,
                )
                for index, _ in enumerate(segment_uids, start=2)
            ]

    refiner = RepeatingRefiner()
    gate = TranscriptionRiskGateService(database)
    metrics = await gate.apply("job-1", refiner)
    async with database.session() as session:
        job = await session.get(AnalysisJob, "job-1")
        assert job is not None
        job.stage = "interrupted"
        await session.commit()

    class EmptyEngine:
        async def transcribe_file(self, _file, _resume_from):
            if False:
                yield None

    await TranscriptionService(
        database,
        risk_gate=gate,
        refiner=refiner,
    ).resume_job("job-1", EmptyEngine())

    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )
    assert len(refiner.calls) == 10
    assert metrics.overflowed == 1
    assert all(item.risk_state == "POST_EDIT_FAILED" for item in rows[2:12])
    assert all(item.is_reliable is False and item.text == "" for item in rows[2:12])
    assert (rows[12].risk_state, rows[12].is_reliable, rows[12].reliability_weight) == (
        None,
        True,
        0.6,
    )
    assert all(item.risk_classified is True for item in rows)
    assert refiner.calls == [f"file-1:{index}" for index in range(2, 12)]
    async with database.session() as session:
        resumed = await session.get(AnalysisJob, "job-1")
    assert resumed is not None
    assert resumed.stage == "analyzing"
    await database.dispose()


@pytest.mark.asyncio
async def test_transcription_does_not_enter_analysis_when_risk_gate_fails(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "risk-gate-stage.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        await session.commit()

    class EmptyEngine:
        async def transcribe_file(self, _file, _resume_from):
            if False:
                yield None

    class FailingRiskGate:
        async def apply(self, _job_id, _refiner, *, bulk_elapsed_seconds):
            raise RuntimeError("risk gate unavailable")

    service = TranscriptionService(
        database,
        risk_gate=FailingRiskGate(),
        refiner=object(),
    )
    with pytest.raises(RuntimeError, match="risk gate unavailable"):
        await service.run_job("job-1", EmptyEngine())

    async with database.session() as session:
        job = await session.get(AnalysisJob, "job-1")
    assert job is not None
    assert job.stage == "interrupted"
    await database.dispose()


@pytest.mark.asyncio
async def test_transcription_without_risk_gate_never_enters_analysis(tmp_path: Path) -> None:
    database = Database(tmp_path / "risk-gate-required.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        await session.commit()

    class EmptyEngine:
        async def transcribe_file(self, _file, _resume_from):
            if False:
                yield None

    with pytest.raises(RuntimeError, match="risk gate"):
        await TranscriptionService(database).run_job("job-1", EmptyEngine())

    async with database.session() as session:
        job = await session.get(AnalysisJob, "job-1")
    assert job is not None
    assert job.stage == "interrupted"
    await database.dispose()


@pytest.mark.asyncio
async def test_refinement_recheck_excludes_hard_rejected_text(tmp_path: Path) -> None:
    database = Database(tmp_path / "rejected-refinement-context.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="h" * 64,
                duration_ms=20_000,
                speech_mapping_json=(
                    '[{"compact_start_ms":10000,"compact_end_ms":16000,'
                    '"source_start_ms":10000,"source_end_ms":16000}]'
                ),
                vad_speech_json='[{"start_ms":10000,"end_ms":16000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        session.add_all(
            [
                Transcript(
                    id=f"rejected-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="discarded result text",
                    words_json="[]",
                )
                for index in range(3)
            ]
            + [
                Transcript(
                    id=f"normal-{index}",
                    job_file_id="file-1",
                    segment_index=index + 3,
                    segment_uid=f"file-1:{index + 3}",
                    start_ms=10_000 + index * 2_000,
                    end_ms=11_000 + index * 2_000,
                    text="repeat candidate text",
                    words_json="[]",
                )
                for index in range(3)
            ]
        )
        await session.commit()

    class ReplacingRefiner:
        async def refine(self, segment_uids: list[str]):
            assert segment_uids == ["file-1:5"]
            return [
                AlignedTranscriptSegment(
                    start_ms=14_000,
                    end_ms=15_000,
                    text="discarded result text",
                    words=(Word("discarded", 14_000, 15_000),),
                    speaker_id=None,
                )
            ]

    await TranscriptionRiskGateService(database).apply("job-1", ReplacingRefiner())
    async with database.session() as session:
        refined = await session.get(Transcript, "normal-2")
    assert refined is not None
    assert (refined.risk_state, refined.is_reliable, refined.text) == (
        "POST_EDIT_PASSED",
        True,
        "discarded result text",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_refinement_result_cannot_shift_to_the_next_queued_segment(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "refinement-result-position.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="i" * 64,
                duration_ms=10_000,
                speech_mapping_json=(
                    '[{"compact_start_ms":0,"compact_end_ms":10000,'
                    '"source_start_ms":0,"source_end_ms":10000}]'
                ),
                vad_speech_json='[{"start_ms":0,"end_ms":10000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="same candidate text",
                    words_json="[]",
                )
                for index in range(4)
            ]
        )
        await session.commit()

    class MissingFirstResultRefiner:
        async def refine(self, segment_uids: list[str]):
            if segment_uids == ["file-1:2"]:
                return []
            assert segment_uids == ["file-1:3"]
            return [
                AlignedTranscriptSegment(
                    start_ms=6_000,
                    end_ms=7_000,
                    text="second queued result",
                    words=(Word("second", 6_000, 7_000),),
                    speaker_id=None,
                )
            ]

    await TranscriptionRiskGateService(database).apply(
        "job-1", MissingFirstResultRefiner()
    )
    async with database.session() as session:
        first = await session.get(Transcript, "transcript-2")
        second = await session.get(Transcript, "transcript-3")
    assert first is not None and second is not None
    assert (first.risk_state, first.text) == ("POST_EDIT_FAILED", "")
    assert (second.risk_state, second.text) == (
        "POST_EDIT_PASSED",
        "second queued result",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_refinement_recheck_rejects_phrase_repetition_after_512_characters(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "post-edit-suffix-repeat.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="j" * 64,
                duration_ms=6_000,
                vad_speech_json='[{"start_ms":0,"end_ms":6000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text="old repeated candidate",
                    words_json="[]",
                )
                for index in range(3)
            ]
        )
        await session.commit()

    prefix = "".join(chr(0x3400 + index) for index in range(520))
    repeated_result = prefix + "风险后缀需要隔离" * 3

    class SuffixRepeatingRefiner:
        async def refine(self, segment_uids: list[str]):
            assert segment_uids == ["file-1:2"]
            return [
                AlignedTranscriptSegment(
                    start_ms=4_000,
                    end_ms=5_000,
                    text=repeated_result,
                    words=(Word(repeated_result, 4_000, 5_000),),
                    speaker_id=None,
                )
            ]

    await TranscriptionRiskGateService(database).apply(
        "job-1", SuffixRepeatingRefiner()
    )
    async with database.session() as session:
        target = await session.get(Transcript, "transcript-2")

    assert target is not None
    assert (target.risk_state, target.is_reliable, target.text) == (
        "POST_EDIT_FAILED",
        False,
        "",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_refinement_recheck_keeps_the_entire_crowded_thirty_second_window(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "post-edit-crowded-window.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="k" * 64,
                duration_ms=26_000,
                vad_speech_json='[{"start_ms":0,"end_ms":26000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        texts = [
            "approximate repeated anchor aa",
            "approximate repeated anchor ab",
            *[chr(0x3400 + index) for index in range(2, 257)],
            "old repeated candidate",
            "old repeated candidate",
            "old repeated candidate",
        ]
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 100,
                    end_ms=index * 100 + 90,
                    text=text,
                    words_json="[]",
                )
                for index, text in enumerate(texts)
            ]
        )
        await session.commit()

    class CrowdedWindowRefiner:
        async def refine(self, segment_uids: list[str]):
            assert segment_uids == ["file-1:259"]
            return [
                AlignedTranscriptSegment(
                    start_ms=25_900,
                    end_ms=25_990,
                    text="approximate repeated anchor ac",
                    words=(Word("approximate repeated anchor ac", 25_900, 25_990),),
                    speaker_id=None,
                )
            ]

    await TranscriptionRiskGateService(database).apply(
        "job-1", CrowdedWindowRefiner()
    )
    async with database.session() as session:
        target = await session.get(Transcript, "transcript-259")

    assert target is not None
    assert (target.risk_state, target.is_reliable, target.text) == (
        "POST_EDIT_FAILED",
        False,
        "",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_refinement_recheck_uses_oversized_rejections_as_repeat_evidence(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "post-edit-oversized-evidence.sqlite3")
    await database.create_schema()
    base = "".join(chr(0x3400 + index) for index in range(1_024))
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="l" * 64,
                duration_ms=10_000,
                vad_speech_json='[{"start_ms":0,"end_ms":10000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        texts = [
            base + "甲",
            base + "乙",
            "old repeated candidate",
            "old repeated candidate",
            "old repeated candidate",
        ]
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=index * 2_000,
                    end_ms=index * 2_000 + 1_000,
                    text=text,
                    words_json="[]",
                )
                for index, text in enumerate(texts)
            ]
        )
        await session.commit()

    class OversizedEvidenceRefiner:
        async def refine(self, segment_uids: list[str]):
            assert segment_uids == ["file-1:4"]
            return [
                AlignedTranscriptSegment(
                    start_ms=8_000,
                    end_ms=9_000,
                    text=base,
                    words=(Word(base, 8_000, 9_000),),
                    speaker_id=None,
                )
            ]

    await TranscriptionRiskGateService(database).apply(
        "job-1", OversizedEvidenceRefiner()
    )
    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(Transcript).order_by(Transcript.segment_index)
            )
        )

    assert [row.risk_reason for row in rows[:2]] == [
        "comparison_text_too_long",
        "comparison_text_too_long",
    ]
    assert all(row.risk_state == "REJECTED" and row.text == "" for row in rows[:2])
    assert (rows[4].risk_state, rows[4].is_reliable, rows[4].text) == (
        "POST_EDIT_FAILED",
        False,
        "",
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_refinement_recheck_uses_budget_rejection_after_old_candidates_expire(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "post-edit-budget-evidence.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="transcribing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="m" * 64,
                duration_ms=31_000,
                vad_speech_json='[{"start_ms":0,"end_ms":31000}]',
                position=0,
                temporary_path=str(tmp_path / "source.mp3"),
            )
        )
        crowded = [
            (
                index,
                index * 100,
                chr(0x3400 + index) + chr(0x5000 + index) + "abcdefghij",
            )
            for index in range(257)
        ]
        crowded[2] = (2, 200, "aaabcdefghij")
        entries = [
            *crowded,
            (257, 25_700, "ababcdefghij"),
            (258, 28_000, "old repeated candidate"),
            (259, 29_000, "old repeated candidate"),
            (260, 30_101, "old repeated candidate"),
        ]
        session.add_all(
            [
                Transcript(
                    id=f"transcript-{index}",
                    job_file_id="file-1",
                    segment_index=index,
                    segment_uid=f"file-1:{index}",
                    start_ms=start_ms,
                    end_ms=start_ms + 90,
                    text=text,
                    words_json="[]",
                )
                for index, start_ms, text in entries
            ]
        )
        await session.commit()

    class BudgetEvidenceRefiner:
        async def refine(self, segment_uids: list[str]):
            assert segment_uids == ["file-1:260"]
            return [
                AlignedTranscriptSegment(
                    start_ms=30_101,
                    end_ms=30_191,
                    text="acabcdefghij",
                    words=(Word("acabcdefghij", 30_101, 30_191),),
                    speaker_id=None,
                )
            ]

    await TranscriptionRiskGateService(database).apply(
        "job-1", BudgetEvidenceRefiner()
    )
    async with database.session() as session:
        rejected = await session.get(Transcript, "transcript-257")
        target = await session.get(Transcript, "transcript-260")

    assert rejected is not None and target is not None
    assert (rejected.risk_state, rejected.risk_reason, rejected.text) == (
        "REJECTED",
        "similarity_comparison_budget_exhausted",
        "",
    )
    assert (target.risk_state, target.is_reliable, target.text) == (
        "POST_EDIT_FAILED",
        False,
        "",
    )
    await database.dispose()
