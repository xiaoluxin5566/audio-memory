import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from audio_memory.diarization.alignment import Word, assign_speakers
from audio_memory.diarization.engine import (
    OfflineDiarizationEngine,
    SherpaOnnxWindowDiarizer,
    SpeakerTurn,
)
from audio_memory.transcription.engine import (
    SpeechInterval,
    VoiceActivityDetector,
    build_speech_mapping,
    map_compact_range,
    valid_chunk_segments,
)


def test_vad_streams_pcm_in_bounded_windows(tmp_path, monkeypatch) -> None:
    reads: list[int] = []

    class FakeStdout:
        chunks = [b"\0" * 1024, b"\0" * 1024, b""]

        def read(self, size: int) -> bytes:
            reads.append(size)
            return self.chunks.pop(0)

        def close(self) -> None:
            pass

    class FakeProcess:
        stdout = FakeStdout()
        stderr = SimpleNamespace(read=lambda: b"")
        returncode = 0

        def wait(self) -> int:
            return 0

    class FakeVadConfig:
        def __init__(self) -> None:
            self.silero_vad = SimpleNamespace(window_size=512)
            self.sample_rate = None

        def validate(self) -> bool:
            return True

    class FakeVad:
        def __init__(self, config, buffer_size_in_seconds: int) -> None:
            assert config.sample_rate == 16_000
            assert buffer_size_in_seconds <= 100
            self._segments = []

        def accept_waveform(self, samples) -> None:
            assert len(samples) == 512

        def flush(self) -> None:
            self._segments.append(SimpleNamespace(start=16_000, samples=[0] * 8_000))

        def empty(self) -> bool:
            return not self._segments

        @property
        def front(self):
            return self._segments[0]

        def pop(self) -> None:
            self._segments.pop(0)

    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(
            VadModelConfig=FakeVadConfig,
            VoiceActivityDetector=FakeVad,
        ),
    )
    monkeypatch.setattr(
        "audio_memory.transcription.engine.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"model")

    intervals = VoiceActivityDetector(model).detect(tmp_path / "five-hours.mp3")

    assert intervals == [SpeechInterval(1_000, 1_500)]
    assert reads and max(reads) == 1024


def test_word_uses_turn_with_largest_overlap() -> None:
    words = [Word("你好", 900, 1300)]
    turns = [
        SpeakerTurn(0, 1000, "speaker_00"),
        SpeakerTurn(1000, 2000, "speaker_01"),
    ]

    assert assign_speakers(words, turns)[0].speaker_id == "speaker_01"


def test_default_segment_timestamp_uses_largest_speaker_overlap() -> None:
    segments = list(
        valid_chunk_segments(
            file_id="file-1",
            chunk_index=0,
            chunk_seconds=300,
            source_offset_ms=10_000,
            raw_segments=[{"start": 0.0, "end": 2.0, "text": "句子"}],
            turns=[
                SpeakerTurn(10_000, 10_500, "speaker_00"),
                SpeakerTurn(10_500, 12_000, "speaker_01"),
            ],
        )
    )

    assert [(item.start_ms, item.end_ms, item.speaker_id) for item in segments] == [
        (10_000, 12_000, "speaker_01")
    ]


def test_compact_range_crossing_join_maps_back_to_source_intervals() -> None:
    mapping = build_speech_mapping(
        [SpeechInterval(1_000, 2_000), SpeechInterval(10_000, 11_000)],
        duration_ms=12_000,
        padding_ms=0,
    )[1]

    assert map_compact_range(900, 1_100, mapping) == [
        SpeechInterval(1_900, 2_000),
        SpeechInterval(10_000, 10_100),
    ]


def test_adjacent_words_with_same_speaker_are_grouped() -> None:
    words = [
        Word("你好", 100, 300),
        Word("世界", 320, 600),
        Word("回应", 700, 1000),
    ]
    turns = [
        SpeakerTurn(0, 650, "speaker_00"),
        SpeakerTurn(650, 1200, "speaker_01"),
    ]

    segments = assign_speakers(words, turns)

    assert [
        (item.start_ms, item.end_ms, item.text, item.speaker_id)
        for item in segments
    ] == [
        (100, 600, "你好世界", "speaker_00"),
        (700, 1000, "回应", "speaker_01"),
    ]


def test_exactly_two_seconds_overlap_allocates_new_global_speaker() -> None:
    def diarize_window(_path, start_ms: int, _duration_ms: int):
        if start_ms == 0:
            return [SpeakerTurn(1_798_000, 1_800_000, "local_a")]
        return [SpeakerTurn(28_000, 33_000, "local_b")]

    engine = OfflineDiarizationEngine(
        duration_probe=lambda _path: 3_570_000,
        window_diarizer=diarize_window,
    )

    turns = engine.diarize("five-hours.mp3")

    assert [turn.speaker_id for turn in turns] == ["speaker_00", "speaker_01"]


def test_more_than_two_seconds_overlap_reuses_global_speaker() -> None:
    def diarize_window(_path, start_ms: int, _duration_ms: int):
        if start_ms == 0:
            return [SpeakerTurn(1_797_000, 1_800_000, "local_a")]
        return [SpeakerTurn(27_000, 33_000, "renamed_local_b")]

    engine = OfflineDiarizationEngine(
        duration_probe=lambda _path: 3_570_000,
        window_diarizer=diarize_window,
    )

    turns = engine.diarize("five-hours.mp3")

    assert [turn.speaker_id for turn in turns] == ["speaker_00", "speaker_00"]


def test_fragmented_overlap_is_accumulated_before_matching_speakers() -> None:
    def diarize_window(_path, start_ms: int, _duration_ms: int):
        if start_ms == 0:
            return [
                SpeakerTurn(1_795_000, 1_796_500, "local_a"),
                SpeakerTurn(1_797_000, 1_798_000, "local_a"),
            ]
        return [
            SpeakerTurn(25_000, 26_500, "renamed_local_b"),
            SpeakerTurn(27_000, 28_000, "renamed_local_b"),
            SpeakerTurn(29_000, 33_000, "renamed_local_b"),
        ]

    engine = OfflineDiarizationEngine(
        duration_probe=lambda _path: 3_570_000,
        window_diarizer=diarize_window,
    )

    turns = engine.diarize("five-hours.mp3")

    assert [turn.speaker_id for turn in turns] == [
        "speaker_00",
        "speaker_00",
        "speaker_00",
    ]


def test_whisper_words_become_absolute_speaker_aware_segments() -> None:
    segments = list(
        valid_chunk_segments(
            file_id="file-1",
            chunk_index=2,
            chunk_seconds=300,
            raw_segments=[
                {
                    "start": 0.9,
                    "end": 1.8,
                    "text": "你好回应",
                    "words": [
                        {"word": "你好", "start": 0.9, "end": 1.3},
                        {"word": "回应", "start": 1.4, "end": 1.8},
                    ],
                }
            ],
            turns=[
                SpeakerTurn(600_800, 601_350, "speaker_00"),
                SpeakerTurn(601_350, 602_000, "speaker_01"),
            ],
        )
    )

    assert [
        (item.start_ms, item.end_ms, item.text, item.speaker_id)
        for item in segments
    ] == [
        (600_900, 601_300, "你好", "speaker_00"),
        (601_400, 601_800, "回应", "speaker_01"),
    ]
    assert segments[1].words == [
        {"word": "回应", "start_ms": 601_400, "end_ms": 601_800}
    ]


def test_sherpa_window_uses_int8_pyannote_and_chinese_embedding(
    tmp_path: Path, monkeypatch
) -> None:
    segmentation = (
        tmp_path / "sherpa-onnx-pyannote-segmentation-3-0" / "model.int8.onnx"
    )
    segmentation.parent.mkdir()
    segmentation.write_bytes(b"segmentation")
    embedding = (
        tmp_path / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
    )
    embedding.write_bytes(b"embedding")

    class Config(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

        def validate(self):
            return True

    class FakeDiarization:
        def __init__(self, config):
            self.config = config

        def process(self, samples):
            valid = (
                self.config.segmentation.pyannote.model == str(segmentation)
                and self.config.embedding.model == str(embedding)
                and samples == [0.25]
            )
            rows = [SimpleNamespace(start=0.5, end=1.25, speaker=3)] if valid else []
            return SimpleNamespace(sort_by_start_time=lambda: rows)

    fake_sherpa = SimpleNamespace(
        OfflineSpeakerDiarizationConfig=Config,
        OfflineSpeakerSegmentationModelConfig=Config,
        OfflineSpeakerSegmentationPyannoteModelConfig=Config,
        SpeakerEmbeddingExtractorConfig=Config,
        FastClusteringConfig=Config,
        OfflineSpeakerDiarization=FakeDiarization,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa)
    diarizer = SherpaOnnxWindowDiarizer(
        segmentation,
        embedding,
        sample_decoder=lambda _path, _start, _duration: [0.25],
    )

    turns = diarizer(Path("local.mp3"), 0, 30_000)

    assert turns == [SpeakerTurn(500, 1250, "local_03")]


def test_five_hour_input_never_decodes_more_than_thirty_minutes(
    tmp_path: Path, monkeypatch
) -> None:
    segmentation = tmp_path / "model.int8.onnx"
    embedding = tmp_path / "3dspeaker.onnx"
    segmentation.write_bytes(b"segmentation")
    embedding.write_bytes(b"embedding")

    class Config(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

        def validate(self):
            return True

    class EmptyDiarization:
        def __init__(self, _config):
            pass

        def process(self, _samples):
            return SimpleNamespace(sort_by_start_time=lambda: [])

    fake_sherpa = SimpleNamespace(
        OfflineSpeakerDiarizationConfig=Config,
        OfflineSpeakerSegmentationModelConfig=Config,
        OfflineSpeakerSegmentationPyannoteModelConfig=Config,
        SpeakerEmbeddingExtractorConfig=Config,
        FastClusteringConfig=Config,
        OfflineSpeakerDiarization=EmptyDiarization,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa)
    decode_calls: list[tuple[int, int]] = []

    def decode_window(_path: Path, start_ms: int, duration_ms: int):
        decode_calls.append((start_ms, duration_ms))
        return [0.0]

    engine = OfflineDiarizationEngine(
        segmentation_model=segmentation,
        embedding_model=embedding,
        duration_probe=lambda _path: 5 * 60 * 60 * 1000,
        sample_decoder=decode_window,
    )

    assert engine.diarize(Path("synthetic-five-hours.mp3")) == []
    assert len(decode_calls) == 11
    assert max(duration for _, duration in decode_calls) == 30 * 60 * 1000
    assert (
        decode_calls[1][0] - decode_calls[0][0]
        == 29 * 60 * 1000 + 30 * 1000
    )


def test_default_diarizer_decodes_local_mp3_for_sherpa(
    tmp_path: Path, monkeypatch
) -> None:
    audio = tmp_path / "short.mp3"
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
            str(audio),
        ],
        check=True,
    )
    segmentation = tmp_path / "model.int8.onnx"
    embedding = tmp_path / "3dspeaker.onnx"
    segmentation.write_bytes(b"segmentation")
    embedding.write_bytes(b"embedding")

    class Config(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

        def validate(self):
            return True

    class LocalDiarization:
        def __init__(self, _config):
            pass

        def process(self, samples):
            rows = (
                [SimpleNamespace(start=0.0, end=0.05, speaker=0)]
                if len(samples) > 0
                else []
            )
            return SimpleNamespace(sort_by_start_time=lambda: rows)

    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(
            OfflineSpeakerDiarizationConfig=Config,
            OfflineSpeakerSegmentationModelConfig=Config,
            OfflineSpeakerSegmentationPyannoteModelConfig=Config,
            SpeakerEmbeddingExtractorConfig=Config,
            FastClusteringConfig=Config,
            OfflineSpeakerDiarization=LocalDiarization,
        ),
    )
    engine = OfflineDiarizationEngine(
        segmentation_model=segmentation,
        embedding_model=embedding,
    )

    turns = engine.diarize(audio)

    assert turns == [SpeakerTurn(0, 50, "speaker_00")]
