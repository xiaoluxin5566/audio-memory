from __future__ import annotations

from dataclasses import dataclass

from audio_memory.prompts.event_schema import EventMap, UserSpeaker


ANALYSIS_WINDOW_GAP_MS = 45_000
ANALYSIS_WINDOW_MAX_SPAN_MS = 1_200_000
ANALYSIS_WINDOW_MAX_SEGMENTS = 400


class AnalysisWindowError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "event_map_coverage_invalid",
    ) -> None:
        super().__init__(message)
        self.code = code


class AnalysisQualityError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__("analysis result did not pass the semantic quality gate")
        self.reason = reason


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


def complete_window_event_map(
    window: AnalysisWindow,
    generated: EventMap,
) -> EventMap:
    event_map = EventMap.model_validate(generated.model_dump(mode="python"))
    segments = {
        str(item["segment_id"]): item
        for item in window.segments
    }
    known_ids = set(segments)
    assigned_ids = {
        segment_id
        for event in event_map.events
        for segment_id in event.evidence_segment_ids
    }
    referenced_ids = assigned_ids | set(event_map.user_speaker.evidence_segment_ids)
    if referenced_ids - known_ids:
        raise AnalysisWindowError(
            "local event map contains unknown evidence",
            code="event_map_unknown_segment",
        )

    for event in event_map.events:
        evidence = [segments[segment_id] for segment_id in event.evidence_segment_ids]
        evidence_start = min(int(item["start_ms"]) for item in evidence)
        evidence_end = max(int(item["end_ms"]) for item in evidence)
        if event.start_ms > evidence_start or event.end_ms < evidence_end:
            raise AnalysisWindowError("local event time range must contain its evidence")
        if event.start_ms < window.start_ms or event.end_ms > window.end_ms:
            raise AnalysisWindowError("local event time range must stay inside its window")

    window_suffix = window.window_id.removeprefix("window_")
    event_ids = {
        event.event_id: (
            f"event_w{window_suffix}_{event.event_id.removeprefix('event_')}"
        )
        for event in event_map.events
    }
    namespaced_events = [
        event.model_copy(
            update={
                "event_id": event_ids[event.event_id],
                "parent_event_id": (
                    event_ids[event.parent_event_id]
                    if event.parent_event_id is not None
                    else None
                ),
            }
        )
        for event in event_map.events
    ]
    return EventMap.model_validate(
        {
            "user_speaker": event_map.user_speaker.model_dump(mode="python"),
            "events": [event.model_dump(mode="python") for event in namespaced_events],
            "unassigned_segment_ids": sorted(known_ids - assigned_ids),
        }
    )


def merge_window_event_maps(
    windows: list[AnalysisWindow],
    maps: list[EventMap],
) -> EventMap:
    if len(windows) != len(maps):
        raise AnalysisWindowError("analysis windows and event maps must align")

    known_ids = {
        str(item["segment_id"])
        for window in windows
        for item in window.segments
    }
    events = [event for event_map in maps for event in event_map.events]
    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise AnalysisWindowError("merged event IDs must be unique")
    assigned_ids = {
        segment_id
        for event in events
        for segment_id in event.evidence_segment_ids
    }
    if assigned_ids - known_ids:
        raise AnalysisWindowError(
            "merged event map contains unknown evidence",
            code="event_map_unknown_segment",
        )

    support: dict[str, list[tuple[str, UserSpeaker]]] = {}
    for window, event_map in zip(windows, maps, strict=True):
        speaker = event_map.user_speaker
        if speaker.speaker_id is None:
            continue
        support.setdefault(speaker.speaker_id, []).append((window.window_id, speaker))
    qualified = {
        speaker_id: candidates
        for speaker_id, candidates in support.items()
        if len({window_id for window_id, _ in candidates}) >= 2
        and min(item.confidence for _, item in candidates) >= 0.85
    }
    if len(qualified) == 1:
        speaker_id, candidates = next(iter(qualified.items()))
        evidence_ids: list[str] = []
        seen_evidence: set[str] = set()
        for _, speaker in candidates:
            for segment_id in speaker.evidence_segment_ids:
                if segment_id not in seen_evidence:
                    evidence_ids.append(segment_id)
                    seen_evidence.add(segment_id)
        user_speaker = UserSpeaker(
            speaker_id=speaker_id,
            confidence=min(item.confidence for _, item in candidates),
            reasoning="同一说话人由至少两个独立分析窗口一致支持。",
            evidence_segment_ids=evidence_ids,
        )
    else:
        user_speaker = UserSpeaker(
            speaker_id=None,
            confidence=0,
            reasoning="没有同一说话人在至少两个独立窗口中达到可靠身份门槛。",
            evidence_segment_ids=[],
        )

    return EventMap.model_validate(
        {
            "user_speaker": user_speaker.model_dump(mode="python"),
            "events": [event.model_dump(mode="python") for event in events],
            "unassigned_segment_ids": sorted(known_ids - assigned_ids),
        }
    )


def validate_analysis_quality(
    transcript: list[dict[str, object]],
    event_map: EventMap,
    results: list[object],
) -> None:
    file_bounds: dict[str, tuple[int, int]] = {}
    for item in transcript:
        file_id = str(item["file_id"])
        start_ms = int(item["start_ms"])
        end_ms = int(item["end_ms"])
        if file_id not in file_bounds:
            file_bounds[file_id] = (start_ms, end_ms)
        else:
            earliest, latest = file_bounds[file_id]
            file_bounds[file_id] = (min(earliest, start_ms), max(latest, end_ms))
    transcript_span_ms = sum(end - start for start, end in file_bounds.values())
    is_long_audio = transcript_span_ms >= 7_200_000

    if is_long_audio and len(event_map.events) == 1:
        raise AnalysisQualityError("long_audio_undersegmented")
    if is_long_audio and transcript_span_ms > 0 and any(
        (event.end_ms - event.start_ms) / transcript_span_ms >= 0.80
        and event.boundary_confidence < 0.70
        for event in event_map.events
    ):
        raise AnalysisQualityError("dominant_low_confidence_event")

    generated = any(
        bool(getattr(result, "should_generate", False))
        and bool(getattr(result, "cards", []) or getattr(result, "todos", []))
        for result in results
    )
    if generated:
        return

    valuable_event_types = {
        "meeting",
        "work_meeting",
        "parenting",
        "family_interaction",
        "commitment",
        "phone_call",
        "discussion",
        "work_session",
        "media",
        "video",
        "podcast",
        "interview",
        "course",
        "speech",
        "news",
        "program",
    }
    if any(event.event_type in valuable_event_types for event in event_map.events):
        raise AnalysisQualityError("valuable_events_all_empty")
    text_characters = sum(len(str(item.get("text", ""))) for item in transcript)
    if text_characters >= 10_000:
        raise AnalysisQualityError("large_transcript_all_empty")
