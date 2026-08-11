from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# This is intentionally independent from the 12,000-character long-route window.
# A normal recording below this limit is sent as its complete original transcript.
AUTONOMOUS_DIRECT_TRANSCRIPT_CHAR_LIMIT = 30_000
AUTONOMOUS_NOTE_WINDOW_TARGET_CHARS = 12_000
AUTONOMOUS_NOTE_WINDOW_HARD_MAX_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class DirectContext:
    route: Literal["direct"]
    transcript: list[dict[str, object]]
    character_count: int


@dataclass(frozen=True, slots=True)
class InformationWindow:
    window_id: str
    segments: list[dict[str, object]]
    character_count: int
    oversized_single_segment: bool


@dataclass(frozen=True, slots=True)
class LongContextPlan:
    route: Literal["long"]
    character_count: int
    windows: tuple[InformationWindow, ...]


AutonomousContext = DirectContext | LongContextPlan


def plan_autonomous_context(
    transcript: list[dict[str, object]],
) -> AutonomousContext:
    """Choose direct source analysis or deterministic long-content windows.

    Windows are an analysis-context concern only. They never split transcription
    segments and have no relationship to local audio Compact batching.
    """

    character_count = sum(len(str(item.get("text", ""))) for item in transcript)
    if character_count <= AUTONOMOUS_DIRECT_TRANSCRIPT_CHAR_LIMIT:
        return DirectContext("direct", transcript, character_count)

    windows: list[InformationWindow] = []
    current: list[dict[str, object]] = []
    current_chars = 0

    def finish_current() -> None:
        nonlocal current, current_chars
        if not current:
            return
        windows.append(
            InformationWindow(
                window_id=f"window_{len(windows) + 1:04d}",
                segments=current,
                character_count=current_chars,
                oversized_single_segment=(
                    len(current) == 1
                    and current_chars > AUTONOMOUS_NOTE_WINDOW_HARD_MAX_CHARS
                ),
            )
        )
        current = []
        current_chars = 0

    for item in transcript:
        segment_chars = len(str(item.get("text", "")))
        if current and current_chars + segment_chars > AUTONOMOUS_NOTE_WINDOW_TARGET_CHARS:
            finish_current()
        current.append(item)
        current_chars += segment_chars
        if current_chars >= AUTONOMOUS_NOTE_WINDOW_HARD_MAX_CHARS:
            finish_current()
    finish_current()

    return LongContextPlan("long", character_count, tuple(windows))
