from audio_memory.transcription.eta import TranscriptionEtaTracker


def test_eta_uses_only_three_latest_valid_samples() -> None:
    tracker = TranscriptionEtaTracker()
    tracker.record("job", 300_000, 30)
    tracker.record("job", 300_000, 20)
    tracker.record("job", 300_000, 10)
    tracker.record("job", 300_000, 5)

    assert tracker.estimate_seconds("job", 600_000) == 23


def test_eta_rejects_invalid_samples() -> None:
    tracker = TranscriptionEtaTracker()
    tracker.record("job", 0, 10)
    tracker.record("job", 300_000, 0)

    assert tracker.estimate_seconds("job", 600_000) is None


def test_eta_clear_discards_existing_samples() -> None:
    tracker = TranscriptionEtaTracker()
    tracker.record("job", 300_000, 30)

    tracker.clear("job")

    assert tracker.estimate_seconds("job", 600_000) is None
