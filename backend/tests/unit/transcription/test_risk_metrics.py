from __future__ import annotations

import logging

from audio_memory.transcription.risk_metrics import (
    RefinementWallClockBudget,
    RiskGateMetrics,
)


def test_metrics_log_only_counts_and_elapsed_without_transcript_content(
    caplog,
) -> None:
    # Adding transcript text, paths, or filenames to the observability event
    # would leak untrusted content outside the risk gate.
    metrics = RiskGateMetrics(
        total_segments=13,
        rejected=1,
        queued=10,
        overflowed=2,
        passed=7,
        failed=3,
        elapsed_seconds=1.25,
    )

    with caplog.at_level(logging.INFO):
        metrics.log(logging.getLogger("risk-gate-metrics"))

    assert caplog.messages == [
        "risk_gate_metrics total_segments=13 rejected=1 queued=10 overflowed=2 "
        "passed=7 failed=3 elapsed_seconds=1.250"
    ]
    assert "秘密文本" not in caplog.text
    assert "source.mp3" not in caplog.text
    assert "/private/audio/source.mp3" not in caplog.text


def test_wall_clock_budget_stops_new_refinement_only_after_current_segment(
) -> None:
    # Allowing a new request after its 20%-of-bulk budget expires would make
    # selective refinement unbounded; an in-flight request must still finish.
    bulk_elapsed = 10.0
    queued_elapsed = 2.0
    budget = RefinementWallClockBudget(bulk_elapsed_seconds=bulk_elapsed)
    metrics = RiskGateMetrics(
        total_segments=3,
        rejected=0,
        queued=1,
        overflowed=2,
        passed=1,
        failed=0,
        elapsed_seconds=queued_elapsed,
    )

    assert budget.allows_next(queued_elapsed_seconds=1.99) is True
    assert budget.allows_next(queued_elapsed_seconds=queued_elapsed) is False
    assert metrics.overflowed == 2
    assert queued_elapsed <= bulk_elapsed * 0.20
    assert budget.limit_seconds == 2.0
