from __future__ import annotations

from collections import defaultdict, deque
import math


class TranscriptionEtaTracker:
    """Keeps short-lived transcription speed samples for active jobs."""

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[int, float]]] = defaultdict(
            lambda: deque(maxlen=3)
        )
        self._progress: dict[str, tuple[str, int, int]] = {}

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

    def set_progress(
        self, job_id: str, phase: str, *, current: int = 0, total: int = 0
    ) -> None:
        self._progress[job_id] = (phase, max(0, current), max(0, total))

    def progress(self, job_id: str) -> tuple[str, int, int] | None:
        return self._progress.get(job_id)
