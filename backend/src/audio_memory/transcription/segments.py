from __future__ import annotations

from dataclasses import dataclass


_VALID_RISK_STATES = frozenset(
    {"REJECTED", "HIGH_RISK_PENDING", "POST_EDIT_PASSED", "POST_EDIT_FAILED"}
)


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    file_id: str
    index: int
    start_ms: int
    end_ms: int
    text: str
    words: list[dict[str, object]]
    risk_state: str | None = None
    is_reliable: bool = True
    reliability_weight: float = 1.0
    risk_reason: str | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("Segment index cannot be negative")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Segment timestamps must be increasing")
        if not self.text.strip():
            raise ValueError("Segment text cannot be blank")
        if self.risk_state is not None and self.risk_state not in _VALID_RISK_STATES:
            raise ValueError("Unknown transcript risk state")


def ordered_text(
    segments: list[TranscriptSegment], file_order: list[str]
) -> str:
    positions = {file_id: index for index, file_id in enumerate(file_order)}
    ordered = sorted(
        segments,
        key=lambda item: (positions[item.file_id], item.index),
    )
    return "\n".join(item.text.strip() for item in ordered)


def progress_percent(*, processed_ms: int, total_ms: int) -> int:
    if total_ms <= 0:
        return 0
    return min(100, max(0, round(processed_ms / total_ms * 100)))
