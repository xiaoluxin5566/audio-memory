from __future__ import annotations

from dataclasses import dataclass


ANALYSIS_WINDOW_GAP_MS = 45_000
ANALYSIS_WINDOW_MAX_SPAN_MS = 1_200_000
ANALYSIS_WINDOW_MAX_SEGMENTS = 400


class AnalysisWindowError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisWindow:
    window_id: str
    file_id: str
    start_ms: int
    end_ms: int
    segments: tuple[dict[str, object], ...]


def build_analysis_windows(
    transcript: list[dict[str, object]],
) -> list[AnalysisWindow]:
    if not transcript:
        return []

    file_order: dict[str, int] = {}
    seen_ids: set[str] = set()
    validated: list[dict[str, object]] = []
    for item in transcript:
        segment_id = str(item.get("segment_id", ""))
        file_id = str(item.get("file_id", ""))
        start_ms = item.get("start_ms")
        end_ms = item.get("end_ms")
        if not segment_id or not file_id:
            raise AnalysisWindowError("analysis segment identity is invalid")
        if segment_id in seen_ids:
            raise AnalysisWindowError("analysis segment IDs must be unique")
        if (
            not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise AnalysisWindowError("analysis segment time range is invalid")
        seen_ids.add(segment_id)
        file_order.setdefault(file_id, len(file_order))
        validated.append(item)

    ordered = sorted(
        validated,
        key=lambda item: (
            file_order[str(item["file_id"])],
            int(item["start_ms"]),
            int(item["end_ms"]),
            str(item["segment_id"]),
        ),
    )
    raw_windows: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    current_file_id = ""
    current_start_ms = 0
    current_end_ms = 0

    for item in ordered:
        file_id = str(item["file_id"])
        start_ms = int(item["start_ms"])
        end_ms = int(item["end_ms"])
        should_split = bool(current) and (
            file_id != current_file_id
            or start_ms - current_end_ms >= ANALYSIS_WINDOW_GAP_MS
            or end_ms - current_start_ms > ANALYSIS_WINDOW_MAX_SPAN_MS
            or len(current) >= ANALYSIS_WINDOW_MAX_SEGMENTS
        )
        if should_split:
            raw_windows.append(current)
            current = []
        if not current:
            current_file_id = file_id
            current_start_ms = start_ms
            current_end_ms = end_ms
        current.append(item)
        current_end_ms = max(current_end_ms, end_ms)

    if current:
        raw_windows.append(current)

    return [
        AnalysisWindow(
            window_id=f"window_{index:04d}",
            file_id=str(items[0]["file_id"]),
            start_ms=min(int(item["start_ms"]) for item in items),
            end_ms=max(int(item["end_ms"]) for item in items),
            segments=tuple(items),
        )
        for index, items in enumerate(raw_windows)
    ]
