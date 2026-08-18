from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import json
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Callable
import unicodedata
from uuid import uuid4

from sqlalchemy import select

from audio_memory.config import AppPaths, WHISPER_MODEL_ID
from audio_memory.db import Database
from audio_memory.diarization.alignment import (
    AlignedTranscriptSegment,
    Word,
    assign_speakers,
)
from audio_memory.diarization.engine import OfflineDiarizationEngine, SpeakerTurn
from audio_memory.models import JobFile, TempFileManifest, Transcript
from audio_memory.transcription.segments import TranscriptSegment
from audio_memory.transcription.compact import (
    CompactBatch,
    CompactCheckpoint,
    LocalFastParameters,
    SourceRange,
    build_compact_batches,
    normalize_source_ranges,
)
from audio_memory.transcription.mapping import (
    MappingRejection,
    MappedSegment,
    map_segment,
    reconcile_mapped_segments,
)
from audio_memory.transcription.metrics import LocalFastMetrics
from audio_memory.transcription.physical_checkpoints import (
    load_physical_chunk_checkpoint,
    physical_checkpoint_fingerprint,
    save_physical_chunk_checkpoint,
)
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.transcription.risk_gate import EnergyInterval
from audio_memory.uploads.cleanup import assert_staging_path


WHISPER_CHUNK_SECONDS = 300
CHUNK_SEGMENT_STRIDE = 10_000
DEFAULT_SPEECH_PADDING_MS = 500
MAX_SPEECH_WINDOW_MS = 30 * 60 * 1000
SPEECH_WINDOW_OVERLAP_MS = 30 * 1000
ENERGY_BUCKET_MS = 1_000
ENERGY_SIGNAL_THRESHOLD = 1e-5
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpeakerAwareTranscriptSegment(TranscriptSegment):
    speaker_id: str | None = None


@dataclass(frozen=True, slots=True)
class SpeechInterval:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Speech interval timestamps must be increasing")


@dataclass(frozen=True, slots=True)
class SpeechMappingEntry:
    compact_start_ms: int
    compact_end_ms: int
    source_start_ms: int
    source_end_ms: int


class FileSpeakerCoordinator:
    """Reconcile window-local speaker labels using source-time overlap only."""

    MIN_REUSE_OVERLAP_MS = 2_000

    def __init__(self) -> None:
        self._next_speaker_index = 0
        self._previous_turns: list[SpeakerTurn] = []

    def coordinate(
        self,
        window: SpeechInterval,
        local_turns: list[SpeakerTurn],
    ) -> list[SpeakerTurn]:
        absolute_turns = [
            SpeakerTurn(
                window.start_ms + turn.start_ms,
                window.start_ms + turn.end_ms,
                turn.speaker_id,
            )
            for turn in local_turns
        ]
        overlap_totals: dict[tuple[str, str], int] = {}
        for previous in self._previous_turns:
            for current in absolute_turns:
                overlap = max(
                    0,
                    min(previous.end_ms, current.end_ms)
                    - max(previous.start_ms, current.start_ms),
                )
                key = (previous.speaker_id, current.speaker_id)
                overlap_totals[key] = overlap_totals.get(key, 0) + overlap

        label_map: dict[str, str] = {}
        used_global: set[str] = set()
        candidates = [
            (overlap, global_id, local_id)
            for (global_id, local_id), overlap in overlap_totals.items()
            if overlap > self.MIN_REUSE_OVERLAP_MS
        ]
        for _, global_id, local_id in sorted(candidates, reverse=True):
            if local_id in label_map or global_id in used_global:
                continue
            label_map[local_id] = global_id
            used_global.add(global_id)

        coordinated: list[SpeakerTurn] = []
        for turn in absolute_turns:
            if turn.speaker_id not in label_map:
                label_map[turn.speaker_id] = (
                    f"speaker_{self._next_speaker_index:02d}"
                )
                self._next_speaker_index += 1
            coordinated.append(
                SpeakerTurn(
                    turn.start_ms,
                    turn.end_ms,
                    label_map[turn.speaker_id],
                )
            )
        self._previous_turns = coordinated
        return coordinated


class VoiceActivityDetector:
    """Stream a local audio file through sherpa-onnx Silero VAD."""

    SAMPLE_RATE = 16_000
    BUFFER_SECONDS = 60

    def __init__(self, model: Path) -> None:
        self.model = model
        self.last_energy_intervals: list[EnergyInterval] = []

    def detect(self, path: Path) -> list[SpeechInterval]:
        if not self.model.is_file():
            raise RuntimeError(f"VAD model is missing: {self.model}")

        import numpy as np
        import sherpa_onnx

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(self.model)
        config.silero_vad.threshold = 0.2
        config.silero_vad.min_silence_duration = 0.25
        config.silero_vad.min_speech_duration = 0.25
        config.silero_vad.max_speech_duration = MAX_SPEECH_WINDOW_MS / 1000
        config.sample_rate = self.SAMPLE_RATE
        if not config.validate():
            raise RuntimeError("Invalid sherpa-onnx VAD configuration")

        vad = sherpa_onnx.VoiceActivityDetector(
            config,
            buffer_size_in_seconds=self.BUFFER_SECONDS,
        )
        window_samples = int(config.silero_vad.window_size)
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-f",
                "s16le",
                "-ar",
                str(self.SAMPLE_RATE),
                "-ac",
                "1",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("Unable to open the VAD audio stream")

        intervals: list[SpeechInterval] = []
        energy_sums: dict[int, float] = {}
        energy_counts: dict[int, int] = {}
        decoded_samples = 0
        samples_per_bucket = self.SAMPLE_RATE * ENERGY_BUCKET_MS // 1000

        def accumulate_energy(samples) -> None:
            nonlocal decoded_samples
            sample_index = 0
            while sample_index < len(samples):
                bucket = (decoded_samples + sample_index) // samples_per_bucket
                bucket_end = (bucket + 1) * samples_per_bucket
                take = min(
                    len(samples) - sample_index,
                    bucket_end - (decoded_samples + sample_index),
                )
                bucket_samples = samples[sample_index : sample_index + take]
                energy_sums[bucket] = energy_sums.get(bucket, 0.0) + float(
                    np.dot(bucket_samples, bucket_samples)
                )
                energy_counts[bucket] = energy_counts.get(bucket, 0) + take
                sample_index += take
            decoded_samples += len(samples)

        def drain_completed_segments() -> None:
            while not vad.empty():
                segment = vad.front
                intervals.append(
                    SpeechInterval(
                        round(segment.start / self.SAMPLE_RATE * 1000),
                        round(
                            (segment.start + len(segment.samples))
                            / self.SAMPLE_RATE
                            * 1000
                        ),
                    )
                )
                vad.pop()

        window_bytes = window_samples * 2
        while raw := process.stdout.read(window_bytes):
            sample_count = len(raw) // 2
            if sample_count == 0:
                continue
            samples = np.frombuffer(raw[: sample_count * 2], dtype=np.int16).astype(
                np.float32
            )
            samples /= 32768.0
            accumulate_energy(samples)
            if sample_count < window_samples:
                samples = np.pad(samples, (0, window_samples - sample_count))
            vad.accept_waveform(samples)
            drain_completed_segments()
        process.stdout.close()
        return_code = process.wait()
        stderr = process.stderr.read()
        if return_code != 0:
            raise RuntimeError(
                f"VAD audio decoding failed ({return_code}): "
                f"{stderr.decode('utf-8', errors='replace')[-200:]}"
            )

        vad.flush()
        drain_completed_segments()
        self.last_energy_intervals = [
            EnergyInterval(
                bucket * ENERGY_BUCKET_MS,
                min(
                    (bucket + 1) * ENERGY_BUCKET_MS,
                    round(decoded_samples / self.SAMPLE_RATE * 1000),
                ),
                energy_sums[bucket] / energy_counts[bucket] >= ENERGY_SIGNAL_THRESHOLD,
            )
            for bucket in sorted(energy_sums)
            if energy_counts[bucket] > 0
            and min(
                (bucket + 1) * ENERGY_BUCKET_MS,
                round(decoded_samples / self.SAMPLE_RATE * 1000),
            )
            > bucket * ENERGY_BUCKET_MS
        ]
        return intervals


def build_speech_mapping(
    intervals: list[SpeechInterval],
    *,
    duration_ms: int,
    padding_ms: int = DEFAULT_SPEECH_PADDING_MS,
) -> tuple[list[SpeechInterval], list[SpeechMappingEntry]]:
    padded = [
        SpeechInterval(item.start_ms, item.end_ms)
        for item in normalize_source_ranges(
            [SourceRange(item.start_ms, item.end_ms) for item in intervals],
            duration_ms=duration_ms,
            padding_ms=padding_ms,
        )
    ]

    windows: list[SpeechInterval] = []
    for interval in padded:
        for start_ms in range(interval.start_ms, interval.end_ms, MAX_SPEECH_WINDOW_MS):
            windows.append(
                SpeechInterval(
                    start_ms,
                    min(interval.end_ms, start_ms + MAX_SPEECH_WINDOW_MS),
                )
            )

    mapping: list[SpeechMappingEntry] = []
    compact_start = 0
    for interval in windows:
        compact_end = compact_start + interval.end_ms - interval.start_ms
        mapping.append(
            SpeechMappingEntry(
                compact_start,
                compact_end,
                interval.start_ms,
                interval.end_ms,
            )
        )
        compact_start = compact_end
    return windows, mapping


def map_compact_range(
    start_ms: int,
    end_ms: int,
    mapping: list[SpeechMappingEntry],
) -> list[SpeechInterval]:
    source_ranges: list[SpeechInterval] = []
    for entry in mapping:
        overlap_start = max(start_ms, entry.compact_start_ms)
        overlap_end = min(end_ms, entry.compact_end_ms)
        if overlap_end <= overlap_start:
            continue
        source_ranges.append(
            SpeechInterval(
                entry.source_start_ms + overlap_start - entry.compact_start_ms,
                entry.source_start_ms + overlap_end - entry.compact_start_ms,
            )
        )
    return source_ranges


def build_processing_windows(
    canonical_windows: list[SpeechInterval],
) -> list[SpeechInterval]:
    processing: list[SpeechInterval] = []
    groups: list[SpeechInterval] = []
    for window in canonical_windows:
        if groups and groups[-1].end_ms == window.start_ms:
            groups[-1] = SpeechInterval(groups[-1].start_ms, window.end_ms)
        else:
            groups.append(window)

    step_ms = MAX_SPEECH_WINDOW_MS - SPEECH_WINDOW_OVERLAP_MS
    for group in groups:
        start_ms = group.start_ms
        while start_ms < group.end_ms:
            end_ms = min(group.end_ms, start_ms + MAX_SPEECH_WINDOW_MS)
            processing.append(SpeechInterval(start_ms, end_ms))
            if end_ms == group.end_ms:
                break
            start_ms += step_ms
    return processing


def build_ownership_windows(
    processing_windows: list[SpeechInterval],
) -> list[SpeechInterval]:
    ownership: list[SpeechInterval] = []
    for index, window in enumerate(processing_windows):
        start_ms = window.start_ms
        end_ms = window.end_ms
        if index and processing_windows[index - 1].end_ms > window.start_ms:
            start_ms = (
                processing_windows[index - 1].end_ms + window.start_ms
            ) // 2
        if (
            index + 1 < len(processing_windows)
            and window.end_ms > processing_windows[index + 1].start_ms
        ):
            end_ms = (
                window.end_ms + processing_windows[index + 1].start_ms
            ) // 2
        ownership.append(SpeechInterval(start_ms, end_ms))
    return ownership


def _normalized_segment_text(text: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", text)
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


_NEGATION_MARKERS = frozenset("不未没无非否莫勿别")
_PROTECTED_NUMBER_TOKEN_PATTERN = re.compile(
    r"(?:(?:版本号?|版)v?|(?<![a-z])(?:version|ver|v))"
    r"[+-]?(?:\d+(?:[.:/-]\d+)*|\.\d+)%?"
    r"|[+-]?(?:\d+(?:[.:/-]\d+)*|\.\d+)%?"
    r"|[零〇一二两三四五六七八九十百千万亿]+"
)
_DATE_TOKEN_PATTERN = re.compile(r"(\d{4})([/\-])(\d{1,2})\2(\d{1,2})")
_TIME_TOKEN_PATTERN = re.compile(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?")
_LEADING_FILLERS = ("嗯", "呃", "额", "哦", "唔")
_TRAILING_PARTICLES = ("啊", "呀", "呢", "吧", "哦", "啦", "嘛", "了")
_SAFE_TEXT_EQUIVALENCES = (("语句", "句子"),)


def _safe_utterance_form(text: str) -> str:
    normalized = _normalized_segment_text(text)
    for variant, canonical in _SAFE_TEXT_EQUIVALENCES:
        normalized = normalized.replace(variant, canonical)
    while normalized.startswith(_LEADING_FILLERS):
        normalized = normalized[1:]
    while normalized.endswith(_TRAILING_PARTICLES):
        normalized = normalized[:-1]
    return normalized.replace("已", "")


def _lightly_normalized_text(text: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", text)
        if not character.isspace()
    )


def _normalize_protected_number_token(token: str) -> str:
    date_match = _DATE_TOKEN_PATTERN.fullmatch(token)
    if date_match is not None:
        year, _, month, day = date_match.groups()
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return f"date:{int(year)}-{int(month)}-{int(day)}"

    time_match = _TIME_TOKEN_PATTERN.fullmatch(token)
    if time_match is not None:
        hour, minute, second = time_match.groups()
        if (
            0 <= int(hour) <= 23
            and 0 <= int(minute) <= 59
            and (second is None or 0 <= int(second) <= 59)
        ):
            parts = (hour, minute) if second is None else (hour, minute, second)
            return "time:" + ":".join(str(int(part)) for part in parts)

    return token


def _protected_number_tokens(text: str) -> tuple[str, ...]:
    lightly_normalized = _lightly_normalized_text(text)
    return tuple(
        _normalize_protected_number_token(match.group())
        for match in _PROTECTED_NUMBER_TOKEN_PATTERN.finditer(lightly_normalized)
    )


def _has_conflicting_protected_tokens(first_text: str, second_text: str) -> bool:
    first_text = _lightly_normalized_text(first_text)
    second_text = _lightly_normalized_text(second_text)
    first_negations = tuple(
        character for character in first_text if character in _NEGATION_MARKERS
    )
    second_negations = tuple(
        character for character in second_text if character in _NEGATION_MARKERS
    )
    if first_negations != second_negations:
        return True
    return _protected_number_tokens(first_text) != _protected_number_tokens(
        second_text
    )


def _utterances_are_same(
    first_start_ms: int,
    first_end_ms: int,
    first_text_value: str,
    second_start_ms: int,
    second_end_ms: int,
    second_text_value: str,
) -> bool:
    time_overlap_ms = max(
        0,
        min(first_end_ms, second_end_ms) - max(first_start_ms, second_start_ms),
    )
    if time_overlap_ms == 0:
        return False
    shorter_duration_ms = min(
        first_end_ms - first_start_ms,
        second_end_ms - second_start_ms,
    )
    if shorter_duration_ms <= 0:
        return False
    if time_overlap_ms / shorter_duration_ms < 0.3:
        return False
    if _has_conflicting_protected_tokens(first_text_value, second_text_value):
        return False
    first_text = _normalized_segment_text(first_text_value)
    second_text = _normalized_segment_text(second_text_value)
    if not first_text or not second_text:
        return False
    return _safe_utterance_form(first_text) == _safe_utterance_form(second_text)


def _segments_are_same_utterance(
    first: TranscriptSegment,
    second: TranscriptSegment,
) -> bool:
    return _utterances_are_same(
        first.start_ms,
        first.end_ms,
        first.text,
        second.start_ms,
        second.end_ms,
        second.text,
    )


def _ownership_contains(
    segment: TranscriptSegment,
    ownership: SpeechInterval,
) -> bool:
    midpoint_ms = (segment.start_ms + segment.end_ms) // 2
    return ownership.start_ms <= midpoint_ms < ownership.end_ms


def reconcile_boundary_segments(
    previous_segments: list[SpeakerAwareTranscriptSegment],
    current_segments: list[SpeakerAwareTranscriptSegment],
    *,
    overlap: SpeechInterval,
    previous_ownership: SpeechInterval,
    current_ownership: SpeechInterval,
) -> tuple[
    list[SpeakerAwareTranscriptSegment],
    list[SpeakerAwareTranscriptSegment],
]:
    def intersects(segment: TranscriptSegment) -> bool:
        return min(segment.end_ms, overlap.end_ms) > max(
            segment.start_ms, overlap.start_ms
        )

    previous_boundary = [item for item in previous_segments if intersects(item)]
    current_boundary = [item for item in current_segments if intersects(item)]
    finalized = [item for item in previous_segments if not intersects(item)]
    remaining = [item for item in current_segments if not intersects(item)]

    candidates: list[
        tuple[float, int, int]
    ] = []
    for previous_index, previous in enumerate(previous_boundary):
        for current_index, current in enumerate(current_boundary):
            if not _segments_are_same_utterance(previous, current):
                continue
            first_text = _normalized_segment_text(previous.text)
            second_text = _normalized_segment_text(current.text)
            similarity = SequenceMatcher(None, first_text, second_text).ratio()
            candidates.append((similarity, previous_index, current_index))

    matched_previous: set[int] = set()
    matched_current: set[int] = set()
    selected: list[SpeakerAwareTranscriptSegment] = []
    for _, previous_index, current_index in sorted(candidates, reverse=True):
        if previous_index in matched_previous or current_index in matched_current:
            continue
        previous = previous_boundary[previous_index]
        current = current_boundary[current_index]
        previous_owned = _ownership_contains(previous, previous_ownership)
        current_owned = _ownership_contains(current, current_ownership)
        if previous_owned != current_owned:
            representative = previous if previous_owned else current
        else:
            representative = max(
                (previous, current),
                key=lambda item: (
                    len(_normalized_segment_text(item.text)),
                    -item.index,
                ),
            )
        selected.append(representative)
        matched_previous.add(previous_index)
        matched_current.add(current_index)

    selected.extend(
        item
        for index, item in enumerate(previous_boundary)
        if index not in matched_previous
    )
    selected.extend(
        item
        for index, item in enumerate(current_boundary)
        if index not in matched_current
    )
    finalized.extend(selected)
    finalized.sort(key=lambda item: (item.index, item.start_ms, item.end_ms))
    return finalized, remaining


def diarize_fail_open(diarization_engine, path: Path) -> list[SpeakerTurn]:
    try:
        return diarization_engine.diarize(path)
    except Exception as exc:
        logger.disabled = False
        logger.warning(
            "Local speaker diarization failed diagnostic=diarization_failed "
            "error_type=%s",
            type(exc).__name__,
        )
        return []


class WhisperBatchResult(list[dict[str, object]]):
    def __init__(
        self,
        segments: list[dict[str, object]],
        *,
        language: str | None,
        language_confidence: float | None,
    ) -> None:
        super().__init__(segments)
        self.language = language
        self.language_confidence = language_confidence


def _transcribe_worker(
    audio_path: str,
    model_id: str,
    word_timestamps: bool = False,
    language: str | None = None,
) -> WhisperBatchResult:
    import mlx_whisper

    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=model_id,
        word_timestamps=word_timestamps,
        condition_on_previous_text=False,
        temperature=0,
        **({"language": language} if language is not None else {}),
    )
    segments = result.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError("Whisper returned an invalid segment list")
    confidence = result.get("language_probability")
    return WhisperBatchResult(
        segments,
        language=result.get("language") if isinstance(result.get("language"), str) else None,
        language_confidence=(
            float(confidence) if isinstance(confidence, (int, float)) else None
        ),
    )


def shift_whisper_segments(
    raw_segments: list[dict[str, object]], *, offset_seconds: float
) -> list[dict[str, object]]:
    """Move timestamps from a physical Whisper subchunk into its compact batch."""
    shifted: list[dict[str, object]] = []
    for raw in raw_segments:
        converted = dict(raw)
        for key in ("start", "end"):
            value = converted.get(key)
            if isinstance(value, (int, float)):
                converted[key] = float(value) + offset_seconds
        words = converted.get("words")
        if isinstance(words, list):
            shifted_words: list[object] = []
            for word in words:
                if not isinstance(word, dict):
                    shifted_words.append(word)
                    continue
                shifted_word = dict(word)
                for key in ("start", "end"):
                    value = shifted_word.get(key)
                    if isinstance(value, (int, float)):
                        shifted_word[key] = float(value) + offset_seconds
                shifted_words.append(shifted_word)
            converted["words"] = shifted_words
        shifted.append(converted)
    return shifted


def prepare_compact_wav(source: Path, batch: CompactBatch, target: Path) -> Path:
    """Materialize one compact batch without copying silence between sources."""
    filters: list[str] = []
    labels: list[str] = []
    for index, entry in enumerate(batch.entries):
        label = f"a{index}"
        labels.append(f"[{label}]")
        if entry.kind == "separator":
            duration = (entry.compact_end_ms - entry.compact_start_ms) / 1000
            filters.append(
                f"anullsrc=r=16000:cl=mono:d={duration:.3f}[{label}]"
            )
        else:
            assert entry.source_start_ms is not None
            assert entry.source_end_ms is not None
            filters.append(
                "[0:a]"
                f"atrim=start={entry.source_start_ms / 1000:.3f}:"
                f"end={entry.source_end_ms / 1000:.3f},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
    filters.append(
        "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Compact WAV preparation failed "
            f"({completed.returncode}): "
            f"{completed.stderr.decode('utf-8', errors='replace')[-200:]}"
        )
    return target


class SelectiveRefiner:
    """Re-decode only selected evidence segments with word timestamps."""

    def __init__(
        self,
        database: Database,
        *,
        model_id: str = WHISPER_MODEL_ID,
        worker: Callable[[str, str, bool], list[dict[str, object]]] = (
            _transcribe_worker
        ),
    ) -> None:
        self.database = database
        self.model_id = model_id
        self.worker = worker

    async def refine(
        self, segment_uids: list[str]
    ) -> list[AlignedTranscriptSegment]:
        if not segment_uids:
            return []
        async with self.database.session() as session:
            rows = await session.execute(
                select(Transcript, JobFile)
                .join(JobFile, JobFile.id == Transcript.job_file_id)
                .where(Transcript.segment_uid.in_(segment_uids))
            )
            by_uid = {
                transcript.segment_uid: (transcript, file)
                for transcript, file in rows.all()
            }

        refined: list[AlignedTranscriptSegment] = []
        for segment_uid in segment_uids:
            pair = by_uid.get(segment_uid)
            if pair is None:
                continue
            transcript, file = pair
            target = Path(file.temporary_path).with_name(
                f".{transcript.id}.{uuid4().hex}.refine.wav"
            )
            try:
                await self._extract_segment(
                    Path(file.temporary_path),
                    target,
                    transcript.start_ms,
                    transcript.end_ms,
                )
                raw_segments = await asyncio.to_thread(
                    self.worker,
                    str(target),
                    self.model_id,
                    True,
                )
                words = self._source_words(transcript, raw_segments)
                if not words:
                    raise RuntimeError("Selective refinement returned no word timestamps")
                text = self._source_text(raw_segments, words)
                if not text:
                    raise RuntimeError("Selective refinement returned no text")
                refined.append(
                    AlignedTranscriptSegment(
                        start_ms=transcript.start_ms,
                        end_ms=transcript.end_ms,
                        text=text,
                        words=tuple(words),
                        speaker_id=transcript.speaker_id,
                    )
                )
            except Exception as exc:
                logger.disabled = False
                logger.warning(
                    "Selective refinement failed "
                    "diagnostic=selective_refinement_failed "
                    "segment_uid=%s error_type=%s",
                    segment_uid,
                    type(exc).__name__,
                )
                refined.append(
                    AlignedTranscriptSegment(
                        start_ms=transcript.start_ms,
                        end_ms=transcript.end_ms,
                        text="",
                        words=(),
                        speaker_id=transcript.speaker_id,
                    )
                )
            finally:
                target.unlink(missing_ok=True)
        return refined

    @staticmethod
    def _source_words(
        transcript: Transcript,
        raw_segments: list[dict[str, object]],
    ) -> list[Word]:
        words: list[Word] = []
        for raw_segment in raw_segments:
            raw_words = raw_segment.get("words", [])
            if not isinstance(raw_words, list):
                continue
            for raw_word in raw_words:
                if not isinstance(raw_word, dict):
                    continue
                start = raw_word.get("start")
                end = raw_word.get("end")
                if not isinstance(start, (int, float)) or not isinstance(
                    end, (int, float)
                ):
                    continue
                start_ms = max(
                    transcript.start_ms,
                    transcript.start_ms + round(float(start) * 1000),
                )
                end_ms = min(
                    transcript.end_ms,
                    transcript.start_ms + round(float(end) * 1000),
                )
                if end_ms <= start_ms:
                    continue
                words.append(
                    Word(
                        str(raw_word.get("word", raw_word.get("text", ""))),
                        start_ms,
                        end_ms,
                    )
                )
        return words

    @staticmethod
    def _source_text(
        raw_segments: list[dict[str, object]], words: list[Word]
    ) -> str:
        text = " ".join(
            str(raw_segment.get("text", "")).strip()
            for raw_segment in raw_segments
            if isinstance(raw_segment, dict)
            and str(raw_segment.get("text", "")).strip()
        ).strip()
        return text or " ".join(
            word.text.strip() for word in words if word.text.strip()
        ).strip()

    @staticmethod
    async def _extract_segment(
        source: Path,
        target: Path,
        start_ms: int,
        end_ms: int,
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{(end_ms - start_ms) / 1000:.3f}",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Selective refinement extraction failed ({process.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[-200:]}"
            )


def chunk_segment(*, file_id: str, chunk_index: int, chunk_seconds: int,
                  local_index: int, raw: dict[str, object],
                  source_offset_ms: int | None = None) -> TranscriptSegment:
    offset_seconds = (
        source_offset_ms / 1000
        if source_offset_ms is not None
        else chunk_index * chunk_seconds
    )
    words = raw.get("words", [])
    shifted_words: list[dict[str, object]] = []
    if isinstance(words, list):
        for word in words:
            if not isinstance(word, dict):
                continue
            start = word.get("start")
            end = word.get("end")
            text = word.get("word", word.get("text", ""))
            if not isinstance(start, (int, float)) or not isinstance(
                end, (int, float)
            ):
                continue
            shifted_words.append(
                {
                    "word": str(text),
                    "start_ms": round((float(start) + offset_seconds) * 1000),
                    "end_ms": round((float(end) + offset_seconds) * 1000),
                }
            )
    return TranscriptSegment(
        file_id=file_id,
        index=chunk_index * CHUNK_SEGMENT_STRIDE + local_index,
        start_ms=round((offset_seconds + float(raw.get("start", 0))) * 1000),
        end_ms=round((offset_seconds + float(raw.get("end", 0))) * 1000),
        text=str(raw.get("text", "")),
        words=shifted_words,
        no_speech_prob=_finite_probability(raw.get("no_speech_prob")),
        avg_logprob=_finite_number(raw.get("avg_logprob")),
    )


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _finite_probability(value: object) -> float | None:
    numeric = _finite_number(value)
    return numeric if numeric is not None and 0.0 <= numeric <= 1.0 else None


def valid_chunk_segments(*, file_id: str, chunk_index: int, chunk_seconds: int,
                         raw_segments: list[dict[str, object]],
                         turns: list[SpeakerTurn] | None = None,
                         source_offset_ms: int | None = None):
    extra_segments = 0
    for local_index, raw in enumerate(raw_segments):
        try:
            segment = chunk_segment(
                file_id=file_id, chunk_index=chunk_index,
                chunk_seconds=chunk_seconds, local_index=local_index, raw=raw,
                source_offset_ms=source_offset_ms,
            )
            if not turns or not segment.words:
                speaker_id = None
                if turns:
                    overlaps = [
                        (
                            max(
                                0,
                                min(segment.end_ms, turn.end_ms)
                                - max(segment.start_ms, turn.start_ms),
                            ),
                            turn.speaker_id,
                        )
                        for turn in turns
                    ]
                    if overlaps and max(overlaps)[0] > 0:
                        speaker_id = max(overlaps)[1]
                yield SpeakerAwareTranscriptSegment(
                    file_id=segment.file_id,
                    index=segment.index,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text,
                    words=segment.words,
                    speaker_id=speaker_id,
                    no_speech_prob=segment.no_speech_prob,
                    avg_logprob=segment.avg_logprob,
                )
                continue
            words = [
                Word(
                    str(item["word"]),
                    int(item["start_ms"]),
                    int(item["end_ms"]),
                )
                for item in segment.words
            ]
            aligned = assign_speakers(words, turns)
            base_index = segment.index + extra_segments
            for aligned_index, item in enumerate(aligned):
                yield SpeakerAwareTranscriptSegment(
                    file_id=file_id,
                    index=base_index + aligned_index,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    text=item.text,
                    words=[
                        {
                            "word": word.text,
                            "start_ms": word.start_ms,
                            "end_ms": word.end_ms,
                        }
                        for word in item.words
                    ],
                    speaker_id=item.speaker_id,
                    no_speech_prob=(
                        segment.no_speech_prob if len(aligned) == 1 else None
                    ),
                    avg_logprob=(segment.avg_logprob if len(aligned) == 1 else None),
                )
            extra_segments += max(0, len(aligned) - 1)
        except (TypeError, ValueError):
            logger.warning(
                "Skipping invalid Whisper segment file=%s chunk=%s segment=%s",
                file_id, chunk_index, local_index,
            )


class MLXWhisperEngine:
    def __init__(
        self,
        database: Database,
        paths: AppPaths,
        *,
        model_id: str = WHISPER_MODEL_ID,
        eta_tracker: TranscriptionEtaTracker | None = None,
        diarization_engine=None,
        voice_activity_detector=None,
        speech_padding_ms: int = DEFAULT_SPEECH_PADDING_MS,
    ) -> None:
        if speech_padding_ms < 0:
            raise ValueError("Speech padding cannot be negative")
        self.database = database
        self.paths = paths
        self.model_id = model_id
        self.eta_tracker = eta_tracker or TranscriptionEtaTracker()
        # Speaker separation is intentionally disabled. Audio is transcribed for
        # information fidelity; the analysis model reasons from linguistic context.
        self.diarization_engine = None
        self.voice_activity_detector = (
            voice_activity_detector
            if voice_activity_detector is not None
            else VoiceActivityDetector(paths.diarization_vad_model)
        )
        self.speech_padding_ms = speech_padding_ms
        self._executor: ProcessPoolExecutor | None = None
        self.metrics_by_job: dict[str, dict[str, object]] = {}

    async def transcribe_file(self, file: JobFile, resume_from: int):
        source = Path(file.temporary_path)
        local_started = time.monotonic()
        metrics = LocalFastMetrics()
        chunk_dir = source.with_name(f"{file.id}.whisper-chunks")
        manifest_id = await self._register(file.job_id, str(uuid4()), chunk_dir)
        completed = False
        try:
            self.eta_tracker.set_progress(file.job_id, "检测语音")
            vad_started = time.monotonic()
            try:
                detected = await asyncio.to_thread(
                    self.voice_activity_detector.detect, source
                )
            except Exception as exc:
                detected = []
                logger.disabled = False
                logger.warning(
                    "Local VAD failed diagnostic=vad_failed error_type=%s",
                    type(exc).__name__,
                )
            metrics.add_timing("vad", time.monotonic() - vad_started)
            vad_available = bool(detected)
            duration_ms = max(
                int(file.duration_ms or 0),
                max((item.end_ms for item in detected), default=0),
            )
            parameters = LocalFastParameters(speech_padding_ms=self.speech_padding_ms)
            self.eta_tracker.set_progress(file.job_id, "整理语音批次")
            ranges = normalize_source_ranges(
                tuple(SourceRange(item.start_ms, item.end_ms) for item in detected),
                duration_ms=duration_ms,
                padding_ms=parameters.speech_padding_ms,
            )
            if not ranges and duration_ms > 0:
                ranges = (SourceRange(0, duration_ms),)
            batches = build_compact_batches(ranges, parameters)
            metrics.add_duration(
                "candidate_speech_ms", sum(batch.speech_ms for batch in batches)
            )
            metrics.add_duration(
                "separator_ms",
                sum(
                    entry.compact_end_ms - entry.compact_start_ms
                    for batch in batches
                    for entry in batch.entries
                    if entry.kind == "separator"
                ),
            )
            await self._persist_vad_speech(file, detected, available=vad_available)
            await self._persist_vad_energy(
                file,
                getattr(self.voice_activity_detector, "last_energy_intervals", []),
            )
            await self._persist_speech_mapping(
                file,
                [
                    SpeechMappingEntry(
                        entry.compact_start_ms,
                        entry.compact_end_ms,
                        entry.source_start_ms,
                        entry.source_end_ms,
                    )
                    for batch in batches
                    for entry in batch.entries
                    if entry.kind == "source"
                    and entry.source_start_ms is not None
                    and entry.source_end_ms is not None
                ],
            )
            chunk_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
            loop = asyncio.get_running_loop()
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=1)
            known_segments = await self._existing_transcript_signatures(file.id)
            checkpoint = CompactCheckpoint.from_json(
                getattr(file, "compact_checkpoint_json", "{}"), parameters
            )
            pending_batches = [
                batch
                for batch in batches
                if batch.index > checkpoint.last_completed_batch
            ]
            prepared: asyncio.Queue[tuple[CompactBatch, Path] | None] = asyncio.Queue(maxsize=2)
            prepared_limit = asyncio.Semaphore(parameters.max_prepared_wavs)
            prepared_count = 0

            async def produce() -> None:
                nonlocal prepared_count
                try:
                    for batch in pending_batches:
                        await prepared_limit.acquire()
                        target = chunk_dir / f"compact-{batch.index:05d}.wav"
                        try:
                            preparation_started = time.monotonic()
                            await asyncio.to_thread(
                                prepare_compact_wav, source, batch, target
                            )
                            metrics.add_timing(
                                "wav_preparation",
                                time.monotonic() - preparation_started,
                            )
                            prepared_count += 1
                            metrics.observe_prepared_wavs(prepared_count)
                            if target.exists():
                                metrics.observe_resources(
                                    peak_rss_bytes=0,
                                    temporary_disk_bytes=target.stat().st_size,
                                )
                        except Exception:
                            prepared_limit.release()
                            raise
                        await prepared.put((batch, target))
                finally:
                    await prepared.put(None)

            producer = asyncio.create_task(produce())
            language_lock = checkpoint.language_lock
            try:
                while True:
                    prepared_item = await prepared.get()
                    if prepared_item is None:
                        break
                    batch, chunk = prepared_item
                    chunk_index = batch.index
                    self.eta_tracker.set_progress(
                        file.job_id,
                        "本地转写",
                        current=chunk_index + 1,
                        total=len(batches),
                        file_id=file.id,
                        unit_ms=batch.speech_ms,
                    )
                    started = time.monotonic()
                    subchunk_dir = chunk.with_name(f"{chunk.stem}-parts")
                    try:
                        max_pcm_chunk_bytes = (
                            parameters.sample_rate_hz
                            * parameters.audio_channels
                            * 2
                            * WHISPER_CHUNK_SECONDS
                            + 44
                        )
                        physical_chunks = (
                            await self._normalize_to_chunks(chunk, subchunk_dir)
                            if chunk.stat().st_size > max_pcm_chunk_bytes
                            else [chunk]
                        )
                        combined_segments: list[dict[str, object]] = []
                        detected_language: str | None = None
                        detected_confidence: float | None = None
                        physical_fingerprint = physical_checkpoint_fingerprint(
                            audio_fingerprint=file.sha256,
                            model_id=self.model_id,
                            parameters_fingerprint=parameters.fingerprint(),
                            batch_index=batch.index,
                        )
                        for part_index, physical_chunk in enumerate(physical_chunks):
                            checkpoint_path = chunk_dir / (
                                f"compact-{batch.index:05d}-part-{part_index:05d}.json"
                            )
                            restored = load_physical_chunk_checkpoint(
                                checkpoint_path,
                                staging_root=self.paths.staging,
                                expected_fingerprint=physical_fingerprint,
                                expected_part_index=part_index,
                            )
                            part_audio_seconds = min(
                                WHISPER_CHUNK_SECONDS,
                                max(
                                    0.0,
                                    batch.compact_ms / 1000
                                    - part_index * WHISPER_CHUNK_SECONDS,
                                ),
                            )
                            if restored is None:
                                part_started = time.monotonic()
                                logger.info(
                                    "Whisper subchunk started job_id=%s file_id=%s "
                                    "batch=%s part=%s/%s audio_seconds=%.1f",
                                    file.job_id, file.id, batch.index + 1,
                                    part_index + 1, len(physical_chunks),
                                    part_audio_seconds,
                                )
                                part_result = await loop.run_in_executor(
                                    self._executor,
                                    _transcribe_worker,
                                    str(physical_chunk),
                                    self.model_id,
                                    parameters.word_timestamps,
                                    language_lock,
                                )
                                part_elapsed = time.monotonic() - part_started
                                logger.info(
                                    "Whisper subchunk completed job_id=%s file_id=%s "
                                    "batch=%s part=%s/%s audio_seconds=%.1f "
                                    "elapsed_seconds=%.1f realtime_factor=%.3f",
                                    file.job_id, file.id, batch.index + 1,
                                    part_index + 1, len(physical_chunks),
                                    part_audio_seconds, part_elapsed,
                                    part_elapsed / part_audio_seconds
                                    if part_audio_seconds > 0 else 0.0,
                                )
                                save_physical_chunk_checkpoint(
                                    checkpoint_path,
                                    staging_root=self.paths.staging,
                                    fingerprint=physical_fingerprint,
                                    part_index=part_index,
                                    segments=list(part_result),
                                    language=part_result.language,
                                    language_confidence=(
                                        part_result.language_confidence
                                    ),
                                )
                                metrics.increment("whisper_calls")
                                metrics.add_timing("whisper", part_elapsed)
                                raw_part_segments = list(part_result)
                                part_language = part_result.language
                                part_confidence = part_result.language_confidence
                            else:
                                raw_part_segments = list(restored["segments"])
                                part_language = restored["language"]
                                part_confidence = restored["language_confidence"]
                                logger.info(
                                    "Whisper subchunk restored job_id=%s file_id=%s "
                                    "batch=%s part=%s/%s",
                                    file.job_id, file.id, batch.index + 1,
                                    part_index + 1, len(physical_chunks),
                                )
                            combined_segments.extend(
                                shift_whisper_segments(
                                    raw_part_segments,
                                    offset_seconds=(
                                        part_index * WHISPER_CHUNK_SECONDS
                                    ),
                                )
                            )
                            if detected_language is None:
                                detected_language = (
                                    str(part_language)
                                    if isinstance(part_language, str) else None
                                )
                                detected_confidence = (
                                    float(part_confidence)
                                    if isinstance(part_confidence, (int, float))
                                    else None
                                )
                            physical_chunk.unlink(missing_ok=True)
                        raw_segments = WhisperBatchResult(
                            combined_segments,
                            language=detected_language,
                            language_confidence=detected_confidence,
                        )
                    finally:
                        chunk.unlink(missing_ok=True)
                        if subchunk_dir.exists():
                            shutil.rmtree(subchunk_dir)
                        prepared_count = max(0, prepared_count - 1)
                        prepared_limit.release()
                    if chunk_index == 0 and (
                        raw_segments.language == parameters.language_lock_code
                        and raw_segments.language_confidence is not None
                        and raw_segments.language_confidence
                        >= parameters.language_lock_confidence
                    ):
                        language_lock = parameters.language_lock_code
                    batch_mapped: list[MappedSegment] = []
                    for local_index, raw in enumerate(raw_segments):
                        if not isinstance(raw, dict):
                            continue
                        converted = dict(raw)
                        if "start_ms" not in converted and isinstance(converted.get("start"), (int, float)):
                            converted["start_ms"] = round(float(converted["start"]) * 1000)
                        if "end_ms" not in converted and isinstance(converted.get("end"), (int, float)):
                            converted["end_ms"] = round(float(converted["end"]) * 1000)
                        mapped = map_segment(
                            batch,
                            converted,
                            tolerance_ms=parameters.mapping_tolerance_ms,
                        )
                        if isinstance(mapped, MappingRejection):
                            metrics.reject_mapping(mapped.reason)
                        elif isinstance(mapped, MappedSegment):
                            midpoint = (mapped.start_ms + mapped.end_ms) // 2
                            owns_midpoint = any(
                                entry.kind == "source"
                                and entry.source_start_ms is not None
                                and entry.source_end_ms is not None
                                and entry.ownership_start_ms is not None
                                and entry.ownership_end_ms is not None
                                and entry.source_start_ms <= midpoint < entry.source_end_ms
                                and entry.ownership_start_ms
                                <= midpoint
                                < entry.ownership_end_ms
                                for entry in batch.entries
                            )
                            if owns_midpoint:
                                batch_mapped.append(mapped)
                    mapping_started = time.monotonic()
                    reconciled = reconcile_mapped_segments(
                        batch_mapped,
                        duplicate_overlap_ratio=parameters.duplicate_overlap_ratio,
                    )
                    metrics.add_timing("mapping", time.monotonic() - mapping_started)
                    if reconciled.conflict_count:
                        metrics.increment("mapping_conflicts", reconciled.conflict_count)
                    for output_index, segment in enumerate(reconciled.kept):
                        candidate = SpeakerAwareTranscriptSegment(
                            file_id=file.id,
                            index=batch.index * CHUNK_SEGMENT_STRIDE + output_index,
                            start_ms=segment.start_ms,
                            end_ms=segment.end_ms,
                            text=segment.text,
                            words=[],
                            speaker_id="unknown",
                            no_speech_prob=segment.no_speech_prob,
                            avg_logprob=segment.avg_logprob,
                        )
                        if self._duplicates_known_segment(candidate, known_segments):
                            continue
                        known_segments.append(
                            (candidate.start_ms, candidate.end_ms, candidate.text.strip())
                        )
                        yield candidate
                    checkpoint = CompactCheckpoint(
                        parameters.fingerprint(),
                        last_completed_batch=batch.index,
                        next_segment_index=(batch.index + 1) * CHUNK_SEGMENT_STRIDE,
                        language_lock=language_lock,
                    )
                    await self._persist_compact_checkpoint(file, checkpoint)
                    for checkpoint_path in chunk_dir.glob(
                        f"compact-{batch.index:05d}-part-*.json"
                    ):
                        checkpoint_path.unlink(missing_ok=True)
                    self.eta_tracker.record(
                        file.job_id,
                        batch.speech_ms,
                        time.monotonic() - started,
                    )
            finally:
                await producer
            self.eta_tracker.set_progress(
                file.job_id,
                "校验时间轴",
                current=len(batches),
                total=len(batches),
                file_id=file.id,
            )
            completed = True
        finally:
            metrics.add_timing("local_total", time.monotonic() - local_started)
            self.metrics_by_job[file.job_id] = metrics.to_dict()
            logger.info(
                "Local fast aggregate metrics job_id=%s metrics=%s",
                file.job_id,
                json.dumps(metrics.to_dict(), sort_keys=True, separators=(",", ":")),
            )
            safe_dir = assert_staging_path(chunk_dir, self.paths.staging)
            if completed and safe_dir.exists():
                shutil.rmtree(safe_dir)
            if completed:
                await self._remove_manifest(manifest_id)

    async def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    @staticmethod
    async def _normalize(source: Path, target: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Audio normalization failed ({process.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[-200:]}"
            )

    @staticmethod
    async def _normalize_to_chunks(source: Path, target_dir: Path) -> list[Path]:
        target_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        pattern = target_dir / "chunk-%05d.wav"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(source),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            "-f", "segment", "-segment_time", str(WHISPER_CHUNK_SECONDS),
            "-reset_timestamps", "1", "-y", str(pattern),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Audio chunking failed ({process.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[-200:]}"
            )
        chunks = sorted(target_dir.glob("chunk-*.wav"))
        if not chunks:
            raise RuntimeError("Audio chunking produced no output")
        return chunks

    @staticmethod
    async def _extract_speech_intervals(
        source: Path,
        target_dir: Path,
        intervals: list[SpeechInterval],
    ) -> list[Path]:
        target_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        clips: list[Path] = []
        for index, interval in enumerate(intervals):
            target = target_dir / f"speech-{index:05d}.wav"
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-loglevel",
                "error",
                "-ss",
                f"{interval.start_ms / 1000:.3f}",
                "-t",
                f"{(interval.end_ms - interval.start_ms) / 1000:.3f}",
                "-i",
                str(source),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(target),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(
                    f"Speech extraction failed ({process.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace')[-200:]}"
                )
            clips.append(target)
        return clips

    async def _persist_speech_mapping(
        self,
        file: JobFile,
        mapping: list[SpeechMappingEntry],
    ) -> None:
        serialized = json.dumps(
            [asdict(item) for item in mapping],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        file.speech_mapping_json = serialized
        async with self.database.session() as session:
            stored = await session.get(JobFile, file.id)
            if stored is not None:
                stored.speech_mapping_json = serialized
                await session.commit()

    async def _persist_compact_checkpoint(
        self, file: JobFile, checkpoint: CompactCheckpoint
    ) -> None:
        serialized = checkpoint.to_json()
        file.compact_checkpoint_json = serialized
        async with self.database.session() as session:
            stored = await session.get(JobFile, file.id)
            if stored is not None:
                stored.compact_checkpoint_json = serialized
                await session.commit()

    async def _persist_vad_speech(
        self,
        file: JobFile,
        intervals: list[SpeechInterval],
        *,
        available: bool,
    ) -> None:
        serialized = json.dumps(
            [asdict(item) for item in intervals],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        file.vad_speech_json = serialized
        file.vad_available = available
        async with self.database.session() as session:
            stored = await session.get(JobFile, file.id)
            if stored is not None:
                stored.vad_speech_json = serialized
                stored.vad_available = available
                await session.commit()

    async def _persist_vad_energy(
        self,
        file: JobFile,
        intervals: list[EnergyInterval],
    ) -> None:
        serialized = json.dumps(
            [asdict(item) for item in intervals],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        file.vad_energy_json = serialized
        async with self.database.session() as session:
            stored = await session.get(JobFile, file.id)
            if stored is not None:
                stored.vad_energy_json = serialized
                await session.commit()

    async def _existing_transcript_signatures(
        self, file_id: str
    ) -> list[tuple[int, int, str]]:
        async with self.database.session() as session:
            rows = await session.execute(
                select(Transcript.start_ms, Transcript.end_ms, Transcript.text).where(
                    Transcript.job_file_id == file_id
                )
            )
            return [
                (int(start_ms), int(end_ms), str(text).strip())
                for start_ms, end_ms, text in rows.all()
            ]

    @staticmethod
    def _duplicates_known_segment(
        segment: TranscriptSegment,
        known_segments: list[tuple[int, int, str]],
    ) -> bool:
        return any(
            _utterances_are_same(
                segment.start_ms,
                segment.end_ms,
                segment.text,
                known_start_ms,
                known_end_ms,
                known_text,
            )
            for known_start_ms, known_end_ms, known_text in known_segments
        )

    async def _register(self, job_id: str, manifest_id: str, path: Path) -> str:
        async with self.database.session() as session:
            existing = await session.scalar(
                select(TempFileManifest).where(
                    TempFileManifest.file_path == str(path)
                )
            )
            if existing is not None:
                if existing.task_uuid != job_id:
                    raise RuntimeError("Temporary path belongs to another job")
                return existing.id
            session.add(
                TempFileManifest(
                    id=manifest_id,
                    task_uuid=job_id,
                    file_path=str(path),
                )
            )
            await session.commit()
            return manifest_id

    async def _remove_manifest(self, manifest_id: str) -> None:
        async with self.database.session() as session:
            record = await session.get(TempFileManifest, manifest_id)
            if record is not None:
                await session.delete(record)
                await session.commit()
