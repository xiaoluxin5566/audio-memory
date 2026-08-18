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


def test_live_batch_fraction_advances_but_stays_below_next_checkpoint() -> None:
    now = [100.0]
    tracker = TranscriptionEtaTracker(clock=lambda: now[0])
    tracker.record("job", 300_000, 30)
    tracker.set_progress(
        "job",
        "本地转写",
        current=2,
        total=4,
        file_id="file-2",
        unit_ms=300_000,
    )

    now[0] += 15
    assert tracker.active_file("job") == "file-2"
    assert tracker.phase_fraction("job") == 0.5

    now[0] += 60
    assert tracker.phase_fraction("job") == 0.95
