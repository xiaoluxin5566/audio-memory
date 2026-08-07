from __future__ import annotations

from dataclasses import dataclass
import logging


@dataclass(frozen=True, slots=True)
class RiskGateMetrics:
    total_segments: int
    rejected: int
    queued: int
    overflowed: int
    passed: int
    failed: int
    elapsed_seconds: float

    def log(self, logger: logging.Logger) -> None:
        """Emit only aggregate, non-content observability fields."""
        logger.info(
            "risk_gate_metrics total_segments=%d rejected=%d queued=%d "
            "overflowed=%d passed=%d failed=%d elapsed_seconds=%.3f",
            self.total_segments,
            self.rejected,
            self.queued,
            self.overflowed,
            self.passed,
            self.failed,
            self.elapsed_seconds,
        )


@dataclass(frozen=True, slots=True)
class RefinementWallClockBudget:
    """Limit selective refinement to a fraction of the bulk-pass wall clock."""

    bulk_elapsed_seconds: float
    fraction: float = 0.20

    @property
    def limit_seconds(self) -> float:
        return max(0.0, self.bulk_elapsed_seconds * self.fraction)

    def allows_next(self, *, queued_elapsed_seconds: float) -> bool:
        return queued_elapsed_seconds < self.limit_seconds
