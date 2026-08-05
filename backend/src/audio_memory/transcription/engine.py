from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import json
import logging
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
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.uploads.cleanup import assert_staging_path


WHISPER_CHUNK_SECONDS = 300
CHUNK_SEGMENT_STRIDE = 10_000
DEFAULT_SPEECH_PADDING_MS = 500
MAX_SPEECH_WINDOW_MS = 30 * 60 * 1000
SPEECH_WINDOW_OVERLAP_MS = 30 * 1000
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
        return intervals


def build_speech_mapping(
    intervals: list[SpeechInterval],
    *,
    duration_ms: int,
    padding_ms: int = DEFAULT_SPEECH_PADDING_MS,
) -> tuple[list[SpeechInterval], list[SpeechMappingEntry]]:
    padded: list[SpeechInterval] = []
    for interval in sorted(intervals, key=lambda item: item.start_ms):
        start_ms = max(0, interval.start_ms - padding_ms)
        end_ms = min(duration_ms, interval.end_ms + padding_ms)
        if padded and start_ms <= padded[-1].end_ms:
            padded[-1] = SpeechInterval(
                padded[-1].start_ms,
                max(padded[-1].end_ms, end_ms),
            )
        else:
            padded.append(SpeechInterval(start_ms, end_ms))

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
_NUMBER_TOKEN_PATTERN = re.compile(
    r"\d+(?:[.:]\d+)?|[零〇一二两三四五六七八九十百千万亿]+"
)
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


def _has_conflicting_protected_tokens(first_text: str, second_text: str) -> bool:
    first_negations = tuple(
        character for character in first_text if character in _NEGATION_MARKERS
    )
    second_negations = tuple(
        character for character in second_text if character in _NEGATION_MARKERS
    )
    if first_negations != second_negations:
        return True
    return tuple(_NUMBER_TOKEN_PATTERN.findall(first_text)) != tuple(
        _NUMBER_TOKEN_PATTERN.findall(second_text)
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
    first_text = _normalized_segment_text(first_text_value)
    second_text = _normalized_segment_text(second_text_value)
    if not first_text or not second_text:
        return False
    if _has_conflicting_protected_tokens(first_text, second_text):
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


def _transcribe_worker(
    audio_path: str,
    model_id: str,
    word_timestamps: bool = False,
) -> list[dict[str, object]]:
    import mlx_whisper

    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=model_id,
        word_timestamps=word_timestamps,
    )
    segments = result.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError("Whisper returned an invalid segment list")
    return segments


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
                refined.append(
                    AlignedTranscriptSegment(
                        start_ms=transcript.start_ms,
                        end_ms=transcript.end_ms,
                        text=transcript.text,
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
                        text=transcript.text,
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
    )


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
        self.diarization_engine = diarization_engine or OfflineDiarizationEngine(
            segmentation_model=paths.diarization_segmentation_model,
            embedding_model=paths.diarization_embedding_model,
        )
        self.voice_activity_detector = (
            voice_activity_detector
            if voice_activity_detector is not None
            else VoiceActivityDetector(paths.diarization_vad_model)
        )
        self.speech_padding_ms = speech_padding_ms
        self._executor: ProcessPoolExecutor | None = None

    async def transcribe_file(self, file: JobFile, resume_from: int):
        source = Path(file.temporary_path)
        chunk_dir = source.with_name(f"{file.id}.whisper-chunks")
        manifest_id = str(uuid4())
        await self._register(file.job_id, manifest_id, chunk_dir)
        try:
            vad_succeeded = True
            try:
                detected = await asyncio.to_thread(
                    self.voice_activity_detector.detect, source
                )
            except Exception as exc:
                vad_succeeded = False
                detected = []
                logger.disabled = False
                logger.warning(
                    "Local VAD failed diagnostic=vad_failed error_type=%s",
                    type(exc).__name__,
                )

            if vad_succeeded:
                duration_ms = max(
                    int(file.duration_ms or 0),
                    max((item.end_ms for item in detected), default=0),
                )
                canonical_intervals, mapping = build_speech_mapping(
                    detected,
                    duration_ms=duration_ms,
                    padding_ms=self.speech_padding_ms,
                )
                await self._persist_speech_mapping(file, mapping)
                speech_intervals = build_processing_windows(canonical_intervals)
                ownership_intervals = build_ownership_windows(speech_intervals)
                chunks = await self._extract_speech_intervals(
                    source, chunk_dir, speech_intervals
                )
                source_offsets = [item.start_ms for item in speech_intervals]
                audio_durations = [
                    item.end_ms - item.start_ms for item in speech_intervals
                ]
            else:
                await self._persist_speech_mapping(file, [])
                chunks = await self._normalize_to_chunks(source, chunk_dir)
                source_offsets = [
                    index * WHISPER_CHUNK_SECONDS * 1000
                    for index in range(len(chunks))
                ]
                source_duration_ms = max(0, int(file.duration_ms or 0))
                audio_durations = [
                    min(
                        WHISPER_CHUNK_SECONDS * 1000,
                        max(
                            0,
                            source_duration_ms
                            - index * WHISPER_CHUNK_SECONDS * 1000,
                        ),
                    )
                    or WHISPER_CHUNK_SECONDS * 1000
                    for index in range(len(chunks))
                ]
                speech_intervals = [
                    SpeechInterval(
                        source_offsets[index],
                        source_offsets[index] + audio_durations[index],
                    )
                    for index in range(len(chunks))
                ]
                ownership_intervals = list(speech_intervals)

            loop = asyncio.get_running_loop()
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=1)
            speaker_coordinator = FileSpeakerCoordinator()
            known_segments = await self._existing_transcript_signatures(file.id)
            pending_segments: list[SpeakerAwareTranscriptSegment] = []
            pending_window_index: int | None = None
            for chunk_index, chunk in enumerate(chunks):
                skip_transcription = (
                    (chunk_index + 1) * CHUNK_SEGMENT_STRIDE <= resume_from
                )
                turns: list[SpeakerTurn] = []
                if vad_succeeded and self.diarization_engine is not None:
                    local_turns = await asyncio.to_thread(
                        diarize_fail_open, self.diarization_engine, chunk
                    )
                    turns = speaker_coordinator.coordinate(
                        speech_intervals[chunk_index], local_turns
                    )
                if skip_transcription:
                    continue
                started = time.monotonic()
                raw_segments = await loop.run_in_executor(
                    self._executor, _transcribe_worker, str(chunk), self.model_id,
                )
                current_segments = [
                    segment
                    for segment in valid_chunk_segments(
                        file_id=file.id,
                        chunk_index=chunk_index,
                        chunk_seconds=WHISPER_CHUNK_SECONDS,
                        raw_segments=raw_segments,
                        turns=turns,
                        source_offset_ms=source_offsets[chunk_index],
                    )
                    if segment.index >= resume_from
                ]
                finalized: list[SpeakerAwareTranscriptSegment] = []
                if pending_window_index is None:
                    pending_segments = current_segments
                else:
                    previous_window = speech_intervals[pending_window_index]
                    current_window = speech_intervals[chunk_index]
                    overlap_start_ms = max(
                        previous_window.start_ms, current_window.start_ms
                    )
                    overlap_end_ms = min(
                        previous_window.end_ms, current_window.end_ms
                    )
                    if overlap_end_ms > overlap_start_ms:
                        finalized, pending_segments = reconcile_boundary_segments(
                            pending_segments,
                            current_segments,
                            overlap=SpeechInterval(
                                overlap_start_ms, overlap_end_ms
                            ),
                            previous_ownership=ownership_intervals[
                                pending_window_index
                            ],
                            current_ownership=ownership_intervals[chunk_index],
                        )
                    else:
                        finalized = pending_segments
                        pending_segments = current_segments
                pending_window_index = chunk_index

                for segment in finalized:
                    if self._duplicates_known_segment(segment, known_segments):
                        continue
                    known_segments.append(
                        (segment.start_ms, segment.end_ms, segment.text.strip())
                    )
                    yield segment

                if current_segments:
                    self.eta_tracker.record(
                        file.job_id,
                        audio_durations[chunk_index],
                        time.monotonic() - started,
                    )

            for segment in pending_segments:
                if self._duplicates_known_segment(segment, known_segments):
                    continue
                known_segments.append(
                    (segment.start_ms, segment.end_ms, segment.text.strip())
                )
                yield segment
        finally:
            safe_dir = assert_staging_path(chunk_dir, self.paths.staging)
            if safe_dir.exists():
                shutil.rmtree(safe_dir)
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

    async def _register(self, job_id: str, manifest_id: str, path: Path) -> None:
        async with self.database.session() as session:
            session.add(
                TempFileManifest(
                    id=manifest_id,
                    task_uuid=job_id,
                    file_path=str(path),
                )
            )
            await session.commit()

    async def _remove_manifest(self, manifest_id: str) -> None:
        async with self.database.session() as session:
            record = await session.get(TempFileManifest, manifest_id)
            if record is not None:
                await session.delete(record)
                await session.commit()
