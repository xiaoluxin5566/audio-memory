from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


WINDOW_MS = 30 * 60 * 1000
WINDOW_OVERLAP_MS = 30 * 1000
MIN_LABEL_OVERLAP_MS = 2 * 1000


def probe_duration_ms(path: Path) -> int:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return max(1, round(float(completed.stdout.strip()) * 1000))


def decode_audio_window(path: Path, start_ms: int, duration_ms: int):
    import numpy as np

    completed = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{duration_ms / 1000:.3f}",
            "-i",
            str(path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "f32le",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(completed.stdout, dtype="<f4").copy()


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker_id: str

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Speaker turn timestamps must be increasing")


class SherpaOnnxWindowDiarizer:
    def __init__(
        self,
        segmentation_model: Path,
        embedding_model: Path,
        *,
        sample_decoder: Callable[[Path, int, int], object],
    ) -> None:
        self.segmentation_model = segmentation_model
        self.embedding_model = embedding_model
        self.sample_decoder = sample_decoder
        self._engine = None

    def __call__(
        self, path: Path, start_ms: int, duration_ms: int
    ) -> list[SpeakerTurn]:
        samples = self.sample_decoder(path, start_ms, duration_ms)
        result = self._sherpa_engine().process(samples).sort_by_start_time()
        return [
            SpeakerTurn(
                round(float(item.start) * 1000),
                round(float(item.end) * 1000),
                f"local_{int(item.speaker):02d}",
            )
            for item in result
        ]

    def _sherpa_engine(self):
        if self._engine is not None:
            return self._engine
        if not self.segmentation_model.is_file() or not self.embedding_model.is_file():
            raise RuntimeError("Local diarization model files are missing")
        import sherpa_onnx

        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(self.segmentation_model)
                )
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.embedding_model)
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=-1,
                threshold=0.5,
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("Local diarization model configuration is invalid")
        self._engine = sherpa_onnx.OfflineSpeakerDiarization(config)
        return self._engine


class OfflineDiarizationEngine:
    def __init__(
        self,
        *,
        segmentation_model: Path | None = None,
        embedding_model: Path | None = None,
        duration_probe: Callable[[Path], int] | None = None,
        window_diarizer: Callable[[Path, int, int], list[SpeakerTurn]] | None = None,
        sample_decoder: Callable[[Path, int, int], object] | None = None,
    ) -> None:
        self._duration_probe = duration_probe
        self.segmentation_model = segmentation_model
        self.embedding_model = embedding_model
        if window_diarizer is not None:
            self._window_diarizer = window_diarizer
        elif segmentation_model is not None and embedding_model is not None:
            self._duration_probe = duration_probe or probe_duration_ms
            self._window_diarizer = SherpaOnnxWindowDiarizer(
                segmentation_model,
                embedding_model,
                sample_decoder=sample_decoder or decode_audio_window,
            )
        else:
            self._window_diarizer = None

    def diarize(self, path: str | Path) -> list[SpeakerTurn]:
        source = Path(path)
        if self._duration_probe is None or self._window_diarizer is None:
            raise RuntimeError("Local diarization models are not configured")
        duration_ms = self._duration_probe(source)
        step_ms = WINDOW_MS - WINDOW_OVERLAP_MS
        output: list[SpeakerTurn] = []
        next_speaker_index = 0
        previous_window_end = 0

        for window_start in range(0, duration_ms, step_ms):
            if previous_window_end >= duration_ms:
                break
            window_duration = min(WINDOW_MS, duration_ms - window_start)
            local_turns = self._window_diarizer(
                source, window_start, window_duration
            )
            absolute_turns = [
                SpeakerTurn(
                    window_start + turn.start_ms,
                    window_start + turn.end_ms,
                    turn.speaker_id,
                )
                for turn in local_turns
            ]
            label_map: dict[str, str] = {}
            if output:
                overlap_totals: dict[tuple[str, str], int] = {}
                overlap_end = window_start + WINDOW_OVERLAP_MS
                for previous in output:
                    for current in absolute_turns:
                        overlap = max(
                            0,
                            min(previous.end_ms, current.end_ms, overlap_end)
                            - max(previous.start_ms, current.start_ms, window_start),
                        )
                        key = (previous.speaker_id, current.speaker_id)
                        overlap_totals[key] = overlap_totals.get(key, 0) + overlap
                overlaps = [
                    (overlap, global_id, local_id)
                    for (global_id, local_id), overlap in overlap_totals.items()
                    if overlap > MIN_LABEL_OVERLAP_MS
                ]
                used_global: set[str] = set()
                for _, global_id, local_id in sorted(overlaps, reverse=True):
                    if local_id not in label_map and global_id not in used_global:
                        label_map[local_id] = global_id
                        used_global.add(global_id)

            for turn in absolute_turns:
                if turn.speaker_id not in label_map:
                    label_map[turn.speaker_id] = f"speaker_{next_speaker_index:02d}"
                    next_speaker_index += 1
                start_ms = max(turn.start_ms, previous_window_end)
                if turn.end_ms <= start_ms:
                    continue
                output.append(
                    SpeakerTurn(start_ms, turn.end_ms, label_map[turn.speaker_id])
                )
            previous_window_end = window_start + window_duration

        return output
