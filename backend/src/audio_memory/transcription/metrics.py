from __future__ import annotations

from dataclasses import dataclass, field
import math


TIMING_KEYS = frozenset(
    {"vad", "normalization", "wav_preparation", "whisper", "mapping", "risk", "local_total"}
)
COUNT_KEYS = frozenset(
    {"whisper_calls", "mapping_conflicts", "hard_rejections", "risk_soft", "risk_hard"}
)
DURATION_KEYS = frozenset(
    {"candidate_speech_ms", "separator_ms", "hard_rejected_speech_ms"}
)


@dataclass(slots=True)
class LocalFastMetrics:
    timings_seconds: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)
    mapping_rejections: dict[str, int] = field(default_factory=dict)
    risk_reasons: dict[str, int] = field(default_factory=dict)
    peak_rss_bytes: int = 0
    temporary_disk_bytes: int = 0
    prepared_wav_high_water: int = 0

    def add_timing(self, stage: str, elapsed_seconds: float) -> None:
        if stage not in TIMING_KEYS or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("Invalid aggregate timing")
        self.timings_seconds[stage] = self.timings_seconds.get(stage, 0.0) + elapsed_seconds

    def increment(self, counter: str, amount: int = 1) -> None:
        if counter not in COUNT_KEYS or amount < 0:
            raise ValueError("Invalid aggregate counter")
        self.counts[counter] = self.counts.get(counter, 0) + amount

    def add_duration(self, name: str, duration_ms: int) -> None:
        if name not in DURATION_KEYS or duration_ms < 0:
            raise ValueError("Invalid aggregate duration")
        self.durations_ms[name] = self.durations_ms.get(name, 0) + duration_ms

    def reject_mapping(self, reason: str) -> None:
        if reason not in {
            "empty_text", "invalid_time", "outside_batch", "separator_only",
            "cross_source_entry", "severe_boundary_overrun",
        }:
            raise ValueError("Invalid mapping rejection reason")
        self.mapping_rejections[reason] = self.mapping_rejections.get(reason, 0) + 1

    def observe_resources(self, *, peak_rss_bytes: int, temporary_disk_bytes: int) -> None:
        self.peak_rss_bytes = max(self.peak_rss_bytes, max(0, peak_rss_bytes))
        self.temporary_disk_bytes = max(self.temporary_disk_bytes, max(0, temporary_disk_bytes))

    def observe_prepared_wavs(self, count: int) -> None:
        self.prepared_wav_high_water = max(self.prepared_wav_high_water, max(0, count))

    def to_dict(self) -> dict[str, object]:
        return {
            "timings_seconds": dict(sorted(self.timings_seconds.items())),
            "counts": dict(sorted(self.counts.items())),
            "durations_ms": dict(sorted(self.durations_ms.items())),
            "mapping_rejections": dict(sorted(self.mapping_rejections.items())),
            "risk_reasons": dict(sorted(self.risk_reasons.items())),
            "peak_rss_bytes": self.peak_rss_bytes,
            "temporary_disk_bytes": self.temporary_disk_bytes,
            "prepared_wav_high_water": self.prepared_wav_high_water,
            "secondary_whisper_calls": 0,
        }
