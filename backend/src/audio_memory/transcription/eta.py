from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
import math
import time


class TranscriptionEtaTracker:
    """Keeps short-lived transcription speed samples for active jobs."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._samples: dict[str, deque[tuple[int, float]]] = defaultdict(
            lambda: deque(maxlen=3)
        )
        self._progress: dict[str, tuple[str, int, int]] = {}
        self._active_files: dict[str, str] = {}
        self._phase_units: dict[str, tuple[int, float]] = {}
        self._direct_fractions: dict[str, float] = {}

    def record(self, job_id: str, audio_ms: int, elapsed_seconds: float) -> None:
        if audio_ms > 0 and elapsed_seconds > 0 and math.isfinite(elapsed_seconds):
            self._samples[job_id].append((audio_ms, elapsed_seconds))

    def estimate_seconds(self, job_id: str, remaining_ms: int) -> int | None:
        samples = self._samples.get(job_id)
        if not samples or remaining_ms < 0:
            return None
        sampled_audio_ms = sum(audio_ms for audio_ms, _ in samples)
        sampled_seconds = sum(seconds for _, seconds in samples)
        return max(0, round(remaining_ms / sampled_audio_ms * sampled_seconds))

    def clear(self, job_id: str) -> None:
        self._samples.pop(job_id, None)
        self._progress.pop(job_id, None)
        self._active_files.pop(job_id, None)
        self._phase_units.pop(job_id, None)
        self._direct_fractions.pop(job_id, None)

    def set_progress(
        self,
        job_id: str,
        phase: str,
        *,
        current: int = 0,
        total: int = 0,
        file_id: str | None = None,
        unit_ms: int = 0,
    ) -> None:
        self._progress[job_id] = (phase, max(0, current), max(0, total))
        if file_id is not None:
            self._active_files[job_id] = file_id
        self._phase_units[job_id] = (max(0, unit_ms), self._clock())
        self._direct_fractions.pop(job_id, None)

    def progress(self, job_id: str) -> tuple[str, int, int] | None:
        return self._progress.get(job_id)

    def active_file(self, job_id: str) -> str | None:
        return self._active_files.get(job_id)

    def set_phase_fraction(self, job_id: str, file_id: str, fraction: float) -> None:
        self._active_files[job_id] = file_id
        self._direct_fractions[job_id] = min(0.95, max(0.0, fraction))

    def phase_fraction(self, job_id: str) -> float:
        if job_id in self._direct_fractions:
            return self._direct_fractions[job_id]
        unit_ms, started_at = self._phase_units.get(job_id, (0, self._clock()))
        samples = self._samples.get(job_id)
        if unit_ms <= 0 or not samples:
            return 0.0
        sampled_audio_ms = sum(audio_ms for audio_ms, _ in samples)
        sampled_seconds = sum(seconds for _, seconds in samples)
        if sampled_audio_ms <= 0 or sampled_seconds <= 0:
            return 0.0
        expected_seconds = unit_ms / sampled_audio_ms * sampled_seconds
        if expected_seconds <= 0:
            return 0.0
        return min(0.95, max(0.0, (self._clock() - started_at) / expected_seconds))
