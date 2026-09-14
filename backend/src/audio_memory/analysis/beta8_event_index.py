from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from audio_memory.prompts.beta8_event_index_schema import (
    Beta8CoverageRange,
    Beta8EmbeddedEventDraft,
    Beta8EventIndex,
    Beta8EventIndexDraft,
    Beta8NormalizedEventIndex,
    Beta8PrimarySession,
    Beta8SegmentRange,
    Beta8TimelineExcludedBlock,
    Beta8TimelineSessionBlock,
)


class EventIndexCoverageError(ValueError):
    """The event index cannot safely route every reliable transcript segment."""


class EventIndexNormalizationError(EventIndexCoverageError):
    """A two-layer draft cannot be converted to a canonical event index."""


@dataclass(frozen=True, slots=True)
class _TranscriptFile:
    key: str
    segment_ids: tuple[str, ...]
    positions: Mapping[str, int]


def _source_aliases(segment: Mapping[str, object]) -> tuple[str, ...]:
    aliases = tuple(
        str(value).strip()
        for value in (
            segment.get("file_id"),
            segment.get("source_file"),
            segment.get("file_name"),
        )
        if value is not None and str(value).strip()
    )
    if not aliases:
        raise EventIndexCoverageError("transcript segment has no source file")
    return aliases


def _reliable_files(transcript: Sequence[Mapping[str, object]]) -> tuple[
    tuple[_TranscriptFile, ...], Mapping[str, int]
]:
    raw_files: list[tuple[list[str], list[str]]] = []
    file_positions: dict[str, int] = {}
    aliases: dict[str, int | None] = {}

    for segment in transcript:
        if segment.get("is_reliable") is False:
            continue
        segment_id = str(segment.get("segment_id") or "").strip()
        if not segment_id:
            raise EventIndexCoverageError("reliable transcript segment has no segment_id")
        source_aliases = _source_aliases(segment)
        canonical = source_aliases[0]
        file_index = file_positions.get(canonical)
        if file_index is None:
            file_index = len(raw_files)
            file_positions[canonical] = file_index
            raw_files.append(([], []))
        segment_ids, known_aliases = raw_files[file_index]
        if segment_id in segment_ids:
            raise EventIndexCoverageError(
                f"duplicate transcript segment ID in source file: {segment_id}"
            )
        segment_ids.append(segment_id)
        known_aliases.extend(source_aliases)

    files: list[_TranscriptFile] = []
    for file_index, (segment_ids, known_aliases) in enumerate(raw_files):
        key = next(key for key, value in file_positions.items() if value == file_index)
        files.append(_TranscriptFile(
            key=key,
            segment_ids=tuple(segment_ids),
            positions={segment_id: position for position, segment_id in enumerate(segment_ids)},
        ))
        for alias in known_aliases:
            previous = aliases.get(alias, file_index)
            aliases[alias] = file_index if previous == file_index else None

    return tuple(files), aliases


def _expand_range(
    segment_range: Beta8SegmentRange,
    *,
    files: Sequence[_TranscriptFile],
    aliases: Mapping[str, int | None],
) -> tuple[tuple[str, str], ...]:
    file_index = aliases.get(segment_range.source_file)
    if file_index is None:
        qualifier = "ambiguous" if segment_range.source_file in aliases else "unknown"
        raise EventIndexCoverageError(
            f"{qualifier} source file: {segment_range.source_file}"
        )
    transcript_file = files[file_index]
    start = transcript_file.positions.get(segment_range.start_segment_id)
    end = transcript_file.positions.get(segment_range.end_segment_id)
    if start is None or end is None:
        unknown = (
            segment_range.start_segment_id if start is None else segment_range.end_segment_id
        )
        raise EventIndexCoverageError(
            f"unknown segment ID in source file {segment_range.source_file}: {unknown}"
        )
    if start > end:
        raise EventIndexCoverageError(
            f"reversed range in source file {segment_range.source_file}: "
            f"{segment_range.start_segment_id} to {segment_range.end_segment_id}"
        )
    return tuple(
        (transcript_file.key, segment_id)
        for segment_id in transcript_file.segment_ids[start : end + 1]
    )


def _system_excluded_ranges(
    transcript: Sequence[Mapping[str, object]],
) -> list[Beta8CoverageRange]:
    by_file: dict[str, list[Mapping[str, object]]] = {}
    for segment in transcript:
        canonical = _source_aliases(segment)[0]
        by_file.setdefault(canonical, []).append(segment)

    exclusions: list[Beta8CoverageRange] = []
    for source_file, segments in by_file.items():
        run: list[str] = []

        def finish_run() -> None:
            if not run:
                return
            exclusions.append(Beta8CoverageRange(
                range=Beta8SegmentRange(
                    source_file=source_file,
                    start_segment_id=run[0],
                    end_segment_id=run[-1],
                ),
                disposition="excluded",
                excluded_reason="unintelligible",
                origin="system",
            ))
            run.clear()

        for segment in segments:
            if segment.get("is_reliable") is False:
                segment_id = str(segment.get("segment_id") or "").strip()
                if not segment_id:
                    raise EventIndexNormalizationError(
                        "unreliable transcript segment has no segment_id"
                    )
                run.append(segment_id)
            else:
                finish_run()
        finish_run()
    return exclusions


def normalize_event_index_draft(
    draft: Beta8EventIndexDraft,
    transcript: Sequence[Mapping[str, object]],
) -> Beta8NormalizedEventIndex:
    """Validate explicit coverage without extending activity boundaries."""
    if not draft.input_complete:
        raise EventIndexNormalizationError(f"input is incomplete: {draft.input_error}")
    files, aliases = _reliable_files(transcript)
    sessions_by_id = {
        session.session_id: session for session in draft.primary_sessions
    }
    timeline_by_file: dict[int, object] = {}
    coverage_ranges: list[Beta8CoverageRange] = []
    ranges_by_session: dict[str, list[Beta8SegmentRange]] = {
        session_id: [] for session_id in sessions_by_id
    }

    for timeline in draft.file_timelines:
        file_index = aliases.get(timeline.source_file)
        if file_index is None:
            qualifier = "ambiguous" if timeline.source_file in aliases else "unknown"
            raise EventIndexNormalizationError(
                f"{qualifier} source file: {timeline.source_file}"
            )
        if file_index in timeline_by_file:
            raise EventIndexNormalizationError(
                f"duplicate file timeline: {timeline.source_file}"
            )
        timeline_by_file[file_index] = timeline
        transcript_file = files[file_index]
        starts: list[int] = []
        for block in timeline.blocks:
            position = transcript_file.positions.get(block.start_segment_id)
            if position is None:
                raise EventIndexNormalizationError(
                    f"unknown timeline start in {timeline.source_file}: "
                    f"{block.start_segment_id}"
                )
            starts.append(position)
        if not starts or starts[0] != 0:
            raise EventIndexNormalizationError(
                f"timeline must start at first reliable segment: {timeline.source_file}"
            )
        if any(current <= previous for previous, current in zip(starts, starts[1:])):
            raise EventIndexNormalizationError(
                f"timeline starts must be strictly increasing: {timeline.source_file}"
            )

        for block_index, block in enumerate(timeline.blocks):
            start = starts[block_index]
            expected_end = (
                starts[block_index + 1] - 1
                if block_index + 1 < len(starts)
                else len(transcript_file.segment_ids) - 1
            )
            end = transcript_file.positions.get(block.end_segment_id)
            if end is None:
                raise EventIndexNormalizationError(
                    f"unknown timeline end: {block.end_segment_id}"
                )
            if end < start:
                raise EventIndexNormalizationError("reversed timeline block range")
            if end != expected_end:
                raise EventIndexNormalizationError(
                    "timeline has a gap or overlap; explicitly account for every "
                    "reliable segment without extending activities"
                )
            segment_range = Beta8SegmentRange(
                source_file=transcript_file.key,
                start_segment_id=transcript_file.segment_ids[start],
                end_segment_id=transcript_file.segment_ids[end],
            )
            if isinstance(block, Beta8TimelineSessionBlock):
                if block.session_id not in sessions_by_id:
                    raise EventIndexNormalizationError(
                        f"unknown primary session: {block.session_id}"
                    )
                ranges_by_session[block.session_id].append(segment_range)
                coverage_ranges.append(Beta8CoverageRange(
                    range=segment_range,
                    disposition="session",
                    session_id=block.session_id,
                    origin="model",
                ))
            elif isinstance(block, Beta8TimelineExcludedBlock):
                coverage_ranges.append(Beta8CoverageRange(
                    range=segment_range,
                    disposition="excluded",
                    excluded_reason=block.excluded_reason,
                    origin="model",
                ))

    missing_timelines = [
        transcript_file.key
        for file_index, transcript_file in enumerate(files)
        if file_index not in timeline_by_file
    ]
    if missing_timelines:
        raise EventIndexNormalizationError(
            f"missing file timeline: {', '.join(missing_timelines)}"
        )

    primary_sessions: list[Beta8PrimarySession] = []
    for session_id, draft_session in sessions_by_id.items():
        ranges = ranges_by_session[session_id]
        if not ranges:
            raise EventIndexNormalizationError(
                f"unreferenced primary session: {session_id}"
            )
        primary_sessions.append(Beta8PrimarySession.model_validate({
            **draft_session.model_dump(mode="json"),
            "ranges": [item.model_dump(mode="json") for item in ranges],
        }))

    parent_segments = {
        session.session_id: {
            segment_key
            for segment_range in session.ranges
            for segment_key in _expand_range(
                segment_range, files=files, aliases=aliases
            )
        }
        for session in primary_sessions
    }
    segment_keys_by_id: dict[str, set[tuple[str, str]]] = {}
    for transcript_file in files:
        for segment_id in transcript_file.segment_ids:
            segment_keys_by_id.setdefault(segment_id, set()).add(
                (transcript_file.key, segment_id)
            )
    ordered_keys = [
        (transcript_file.key, segment_id)
        for transcript_file in files
        for segment_id in transcript_file.segment_ids
    ]
    key_positions = {key: position for position, key in enumerate(ordered_keys)}
    for session in primary_sessions:
        owned = parent_segments[session.session_id]
        positions = [key_positions[key] for key in owned]
        # Context supports a transition; it never changes segment ownership.
        # Do not infer adjacency between separate source files.
        allowed_by_boundary = {"start": set(owned), "end": set(owned)}
        for boundary, edge, offset in (
            ("start", min(positions), -1),
            ("end", max(positions), 1),
        ):
            neighbor = edge + offset
            if (
                0 <= neighbor < len(ordered_keys)
                and ordered_keys[neighbor][0] == ordered_keys[edge][0]
            ):
                allowed_by_boundary[boundary].add(ordered_keys[neighbor])
        for boundary, evidence_id in (
            [("start", value) for value in session.start_boundary_evidence_segment_ids]
            + [("end", value) for value in session.end_boundary_evidence_segment_ids]
        ):
            evidence_keys = segment_keys_by_id.get(evidence_id)
            if not evidence_keys:
                raise EventIndexNormalizationError(
                    f"boundary evidence is unknown for {session.session_id}: "
                    f"{evidence_id}"
                )
            if not evidence_keys & allowed_by_boundary[boundary]:
                raise EventIndexNormalizationError(
                    "boundary evidence is outside primary session "
                    f"{session.session_id}: {evidence_id}"
                )
    normalized_embedded_events: list[Beta8EmbeddedEventDraft] = []
    for event in draft.embedded_events:
        if event.parent_session_id not in parent_segments:
            raise EventIndexNormalizationError(
                f"unknown embedded event parent: {event.parent_session_id}"
            )
        evidence_segments = {
            segment_key
            for segment_range in event.evidence_ranges
            for segment_key in _expand_range(
                segment_range, files=files, aliases=aliases
            )
        }
        if evidence_segments <= parent_segments[event.parent_session_id]:
            normalized_embedded_events.append(event)
            continue
        candidate_parents = [
            session_id
            for session_id, owned_segments in parent_segments.items()
            if evidence_segments <= owned_segments
        ]
        if len(candidate_parents) != 1:
            raise EventIndexNormalizationError(
                f"embedded event {event.event_id} is outside one primary session"
            )
        normalized_embedded_events.append(
            event.model_copy(update={"parent_session_id": candidate_parents[0]})
        )

    return Beta8NormalizedEventIndex(
        input_complete=True,
        input_error=None,
        primary_sessions=primary_sessions,
        embedded_events=normalized_embedded_events,
        coverage_ranges=coverage_ranges,
        system_excluded_ranges=_system_excluded_ranges(transcript),
    )


def validate_event_index(
    index: Beta8EventIndex, transcript: Sequence[Mapping[str, object]]
) -> None:
    """Verify structural closure against the reliable input transcript only."""
    if index.input_complete and index.input_error is not None:
        raise EventIndexCoverageError("input_error must be null when input_complete is true")
    if not index.input_complete:
        if not (index.input_error or "").strip():
            raise EventIndexCoverageError("input_error is required when input_complete is false")
        raise EventIndexCoverageError(f"input is incomplete: {index.input_error}")

    files, aliases = _reliable_files(transcript)
    covered: dict[tuple[str, str], str] = {}
    unit_ids: set[str] = set()

    def record(segment_keys: Sequence[tuple[str, str]], owner: str) -> None:
        for segment_key in segment_keys:
            previous = covered.get(segment_key)
            if previous is not None:
                raise EventIndexCoverageError(
                    f"coverage overlap for segment ID {segment_key[1]}: {previous}, {owner}"
                )
            covered[segment_key] = owner

    for unit in index.units:
        if unit.unit_id in unit_ids:
            raise EventIndexCoverageError(f"duplicate unit_id: {unit.unit_id}")
        unit_ids.add(unit.unit_id)
        for position, segment_range in enumerate(unit.ranges):
            record(
                _expand_range(segment_range, files=files, aliases=aliases),
                f"unit {unit.unit_id} range {position}",
            )
    for position, excluded in enumerate(index.excluded_ranges):
        record(
            _expand_range(excluded.range, files=files, aliases=aliases),
            f"excluded range {position}",
        )

    missing = [
        f"{transcript_file.key}:{segment_id}"
        for transcript_file in files
        for segment_id in transcript_file.segment_ids
        if (transcript_file.key, segment_id) not in covered
    ]
    if missing:
        raise EventIndexCoverageError(f"coverage gap: {', '.join(missing)}")
