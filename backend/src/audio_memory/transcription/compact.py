from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json


@dataclass(frozen=True, slots=True)
class LocalFastParameters:
    """Frozen V0.1 parameters that fence compact checkpoints and benchmarks."""

    vad_threshold: float = 0.2
    min_speech_ms: int = 250
    min_silence_ms: int = 250
    max_speech_ms: int = 1_800_000
    speech_padding_ms: int = 500
    vad_buffer_seconds: int = 60
    sample_rate_hz: int = 16_000
    audio_channels: int = 1
    first_batch_target_speech_ms: int = 180_000
    first_batch_min_speech_ms: int = 120_000
    later_batch_target_speech_ms: int = 900_000
    later_batch_min_speech_ms: int = 600_000
    max_batch_speech_ms: int = 1_200_000
    separator_ms: int = 500
    forced_split_overlap_ms: int = 1_500
    max_prepared_wavs: int = 2
    whisper_workers: int = 1
    whisper_model_id: str = "mlx-community/whisper-large-v3-turbo"
    word_timestamps: bool = False
    condition_on_previous_text: bool = False
    temperature: float = 0.0
    language_lock_code: str = "zh"
    language_lock_confidence: float = 0.9
    mapping_tolerance_ms: int = 300
    duplicate_overlap_ratio: float = 0.3
    secondary_whisper_budget: int = 0

    def canonical_dict(self) -> dict[str, object]:
        return asdict(self)

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceRange:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Source range timestamps must be positive and increasing")


@dataclass(frozen=True, slots=True)
class CompactEntry:
    compact_start_ms: int
    compact_end_ms: int
    source_start_ms: int | None
    source_end_ms: int | None
    kind: str
    ownership_start_ms: int | None
    ownership_end_ms: int | None

    def __post_init__(self) -> None:
        if self.compact_start_ms < 0 or self.compact_end_ms <= self.compact_start_ms:
            raise ValueError("Compact entry timestamps must be increasing")
        if self.kind not in {"source", "separator"}:
            raise ValueError("Compact entry kind is invalid")
        source_values = (
            self.source_start_ms,
            self.source_end_ms,
            self.ownership_start_ms,
            self.ownership_end_ms,
        )
        if self.kind == "separator" and any(item is not None for item in source_values):
            raise ValueError("Separator entries cannot map to source audio")
        if self.kind == "source":
            if any(item is None for item in source_values):
                raise ValueError("Source entries require source and ownership ranges")
            assert self.source_start_ms is not None
            assert self.source_end_ms is not None
            assert self.ownership_start_ms is not None
            assert self.ownership_end_ms is not None
            if not (
                0 <= self.source_start_ms < self.source_end_ms
                and self.source_start_ms
                <= self.ownership_start_ms
                < self.ownership_end_ms
                <= self.source_end_ms
            ):
                raise ValueError("Source entry ranges are invalid")

    @classmethod
    def separator(cls, compact_start_ms: int, compact_end_ms: int) -> CompactEntry:
        return cls(
            compact_start_ms,
            compact_end_ms,
            None,
            None,
            "separator",
            None,
            None,
        )


@dataclass(frozen=True, slots=True)
class CompactBatch:
    index: int
    entries: tuple[CompactEntry, ...]
    speech_ms: int
    compact_ms: int
    forced_split: bool
    parameter_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CompactBatch:
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, (list, tuple)):
            raise ValueError("Compact batch entries are required")
        return cls(
            index=int(payload["index"]),
            entries=tuple(CompactEntry(**dict(item)) for item in raw_entries),
            speech_ms=int(payload["speech_ms"]),
            compact_ms=int(payload["compact_ms"]),
            forced_split=bool(payload["forced_split"]),
            parameter_fingerprint=str(payload["parameter_fingerprint"]),
        )


@dataclass(frozen=True, slots=True)
class CompactCheckpoint:
    parameter_fingerprint: str
    last_completed_batch: int = -1
    next_segment_index: int = 0
    language_lock: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(
        cls, raw: str | None, parameters: LocalFastParameters
    ) -> CompactCheckpoint:
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict) or not payload:
            return cls(parameters.fingerprint())
        fingerprint = payload.get("parameter_fingerprint")
        if fingerprint != parameters.fingerprint():
            raise ValueError("Compact checkpoint parameter fingerprint mismatch")
        last_completed = int(payload.get("last_completed_batch", -1))
        next_index = int(payload.get("next_segment_index", 0))
        language_lock = payload.get("language_lock")
        if last_completed < -1 or next_index < 0 or language_lock not in {None, "zh"}:
            raise ValueError("Compact checkpoint is invalid")
        return cls(str(fingerprint), last_completed, next_index, language_lock)


@dataclass(frozen=True, slots=True)
class _SourcePiece:
    start_ms: int
    end_ms: int
    ownership_start_ms: int
    ownership_end_ms: int
    forced_split: bool = False

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _split_source_ranges(
    ranges: tuple[SourceRange, ...], parameters: LocalFastParameters
) -> list[_SourcePiece]:
    pieces: list[_SourcePiece] = []
    for source_range in ranges:
        if source_range.end_ms - source_range.start_ms <= parameters.max_batch_speech_ms:
            pieces.append(
                _SourcePiece(
                    source_range.start_ms,
                    source_range.end_ms,
                    source_range.start_ms,
                    source_range.end_ms,
                )
            )
            continue

        split_ranges: list[tuple[int, int]] = []
        start_ms = source_range.start_ms
        while start_ms < source_range.end_ms:
            end_ms = min(
                source_range.end_ms, start_ms + parameters.max_batch_speech_ms
            )
            split_ranges.append((start_ms, end_ms))
            if end_ms == source_range.end_ms:
                break
            start_ms = end_ms - parameters.forced_split_overlap_ms

        ownership_boundaries = [
            (left[1] + right[0]) // 2
            for left, right in zip(split_ranges, split_ranges[1:])
        ]
        for index, (start_ms, end_ms) in enumerate(split_ranges):
            pieces.append(
                _SourcePiece(
                    start_ms,
                    end_ms,
                    source_range.start_ms if index == 0 else ownership_boundaries[index - 1],
                    source_range.end_ms if index == len(split_ranges) - 1 else ownership_boundaries[index],
                    True,
                )
            )
    return pieces


def _make_batch(
    index: int,
    pieces: list[_SourcePiece],
    parameters: LocalFastParameters,
) -> CompactBatch:
    entries: list[CompactEntry] = []
    compact_cursor = 0
    previous: _SourcePiece | None = None
    for piece in pieces:
        if previous is not None and piece.start_ms > previous.end_ms:
            entries.append(
                CompactEntry.separator(
                    compact_cursor, compact_cursor + parameters.separator_ms
                )
            )
            compact_cursor += parameters.separator_ms
        compact_end = compact_cursor + piece.duration_ms
        entries.append(
            CompactEntry(
                compact_cursor,
                compact_end,
                piece.start_ms,
                piece.end_ms,
                "source",
                piece.ownership_start_ms,
                piece.ownership_end_ms,
            )
        )
        compact_cursor = compact_end
        previous = piece
    return CompactBatch(
        index=index,
        entries=tuple(entries),
        speech_ms=sum(item.duration_ms for item in pieces),
        compact_ms=compact_cursor,
        forced_split=any(item.forced_split for item in pieces),
        parameter_fingerprint=parameters.fingerprint(),
    )


def build_compact_batches(
    ranges: tuple[SourceRange, ...], parameters: LocalFastParameters
) -> tuple[CompactBatch, ...]:
    if not ranges:
        return ()
    pieces = _split_source_ranges(ranges, parameters)
    groups: list[list[_SourcePiece]] = []
    current: list[_SourcePiece] = []
    current_speech_ms = 0

    def flush() -> None:
        nonlocal current, current_speech_ms
        if current:
            groups.append(current)
            current = []
            current_speech_ms = 0

    for piece in pieces:
        if piece.forced_split:
            flush()
            groups.append([piece])
            continue
        target_ms = (
            parameters.first_batch_target_speech_ms
            if not groups
            else parameters.later_batch_target_speech_ms
        )
        if current and (
            current_speech_ms >= target_ms
            or current_speech_ms + piece.duration_ms > parameters.max_batch_speech_ms
        ):
            flush()
        current.append(piece)
        current_speech_ms += piece.duration_ms
    flush()

    if len(groups) > 1:
        final_speech_ms = sum(item.duration_ms for item in groups[-1])
        minimum_ms = parameters.later_batch_min_speech_ms
        previous_speech_ms = sum(item.duration_ms for item in groups[-2])
        if (
            final_speech_ms < minimum_ms
            and previous_speech_ms + final_speech_ms
            <= parameters.max_batch_speech_ms
            and not any(item.forced_split for item in groups[-2] + groups[-1])
        ):
            groups[-2].extend(groups.pop())

    return tuple(
        _make_batch(index, group, parameters) for index, group in enumerate(groups)
    )


def normalize_source_ranges(
    intervals: list[SourceRange] | tuple[SourceRange, ...],
    *,
    duration_ms: int,
    padding_ms: int = 500,
) -> tuple[SourceRange, ...]:
    if duration_ms < 0:
        raise ValueError("Duration cannot be negative")
    if padding_ms < 0:
        raise ValueError("Padding cannot be negative")

    normalized: list[SourceRange] = []
    for interval in sorted(intervals, key=lambda item: (item.start_ms, item.end_ms)):
        start_ms = max(0, interval.start_ms - padding_ms)
        end_ms = min(duration_ms, interval.end_ms + padding_ms)
        if end_ms <= start_ms:
            continue
        if normalized and start_ms <= normalized[-1].end_ms:
            normalized[-1] = SourceRange(
                normalized[-1].start_ms,
                max(normalized[-1].end_ms, end_ms),
            )
        else:
            normalized.append(SourceRange(start_ms, end_ms))
    return tuple(normalized)
