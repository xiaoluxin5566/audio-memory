from __future__ import annotations

from collections import defaultdict, deque


class TranscriptionEtaTracker:
    """Keeps short-lived transcription speed samples for active jobs."""

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[int, float]]] = defaultdict(
            lambda: deque(maxlen=3)
        )

    def record(self, job_id: str, audio_ms: int, elapsed_seconds: float) -> None:
        if audio_ms > 0 and elapsed_seconds > 0:
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
