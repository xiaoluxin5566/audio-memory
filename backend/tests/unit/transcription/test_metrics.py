import json
import math

from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.transcription.metrics import LocalFastMetrics


def test_metrics_serialize_only_aggregate_allowlisted_values() -> None:
    metrics = LocalFastMetrics()
    metrics.add_timing("whisper", 12.5)
    metrics.increment("whisper_calls", 2)
    metrics.add_duration("candidate_speech_ms", 120_000)
    metrics.observe_resources(peak_rss_bytes=1234, temporary_disk_bytes=5678)
    metrics.observe_prepared_wavs(2)

    serialized = json.dumps(metrics.to_dict(), ensure_ascii=False)

    assert metrics.to_dict()["timings_seconds"] == {"whisper": 12.5}
    assert metrics.to_dict()["counts"]["whisper_calls"] == 2
    assert metrics.to_dict()["secondary_whisper_calls"] == 0
    assert "private-source" not in serialized
    assert "transcript" not in serialized
    assert "audio_bytes" not in serialized


def test_eta_uses_only_latest_three_finite_positive_compact_batches() -> None:
    tracker = TranscriptionEtaTracker()
    tracker.record("job", 100_000, 10)
    tracker.record("job", 200_000, 30)
    tracker.record("job", 300_000, 60)
    tracker.record("job", 400_000, 100)
    tracker.record("job", 0, 99)
    tracker.record("job", 100_000, math.inf)
    tracker.record("job", 100_000, math.nan)

    assert tracker.estimate_seconds("job", 900_000) == 190
