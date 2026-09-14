from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from audio_memory.analysis.beta8_event_index import (
    _expand_range,
    _reliable_files,
    _source_aliases,
    _system_excluded_ranges,
)
from audio_memory.prompts.beta8_event_index_schema import (
    Beta8CoverageRange,
    Beta8EmbeddedEventDraft,
    Beta8NormalizedEventIndex,
    Beta8PrimarySession,
    Beta8SegmentRange,
)


class EventIndexWindowMergeError(ValueError):
    """Window indexes cannot be merged without inventing timeline facts."""


@dataclass(frozen=True, slots=True)
class Beta8IndexWindow:
    name: str
    index: Beta8NormalizedEventIndex
    supplied_transcript: Sequence[Mapping[str, object]]
    core_transcript: Sequence[Mapping[str, object]]


Owner = tuple[str, str]
Node = tuple[int, str]
SegmentKey = tuple[str, str]


def _segment_key(segment: Mapping[str, object]) -> SegmentKey:
    segment_id = str(segment.get("segment_id") or "").strip()
    if not segment_id:
        raise EventIndexWindowMergeError("transcript segment has no segment_id")
    return _source_aliases(segment)[0], segment_id


def _runs(
    keys: Sequence[SegmentKey],
    *,
    positions: Mapping[SegmentKey, int],
) -> list[Beta8SegmentRange]:
    ordered = sorted(set(keys), key=positions.__getitem__)
    if not ordered:
        return []
    result: list[Beta8SegmentRange] = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        contiguous = (
            current[0] == previous[0]
            and positions[current] == positions[previous] + 1
        )
        if not contiguous:
            result.append(Beta8SegmentRange(
                source_file=start[0],
                start_segment_id=start[1],
                end_segment_id=previous[1],
            ))
            start = current
        previous = current
    result.append(Beta8SegmentRange(
        source_file=start[0],
        start_segment_id=start[1],
        end_segment_id=previous[1],
    ))
    return result


def _merge_text(values: Sequence[str], maximum: int) -> str:
    output = ""
    for value in dict.fromkeys(item.strip() for item in values if item.strip()):
        candidate = value if not output else f"{output}；{value}"
        if len(candidate) > maximum:
            break
        output = candidate
    return output or values[0][:maximum]


def _session_signature(session: Beta8PrimarySession) -> tuple[str, str]:
    return (
        session.activity_kind,
        session.participation_mode,
    )


def _merge_communication_purpose(
    sessions: Sequence[Beta8PrimarySession],
) -> str:
    purposes = {item.communication_purpose for item in sessions}
    if purposes == {"not_applicable"}:
        return "not_applicable"
    purposes.discard("uncertain")
    if not purposes:
        return "uncertain"
    if "mixed" in purposes or purposes == {"work", "non_work"}:
        return "mixed"
    if len(purposes) == 1:
        return next(iter(purposes))
    raise EventIndexWindowMergeError(
        f"cannot merge communication purposes: {sorted(purposes)}"
    )


def merge_event_index_windows(
    windows: Sequence[Beta8IndexWindow],
    full_transcript: Sequence[Mapping[str, object]],
) -> Beta8NormalizedEventIndex:
    """Merge context-overlapped indexes while giving each core segment one owner."""
    if not windows:
        raise EventIndexWindowMergeError("at least one index window is required")
    files, aliases = _reliable_files(full_transcript)
    full_keys = [
        (transcript_file.key, segment_id)
        for transcript_file in files
        for segment_id in transcript_file.segment_ids
    ]
    positions = {key: position for position, key in enumerate(full_keys)}

    core_keys_by_window: list[tuple[SegmentKey, ...]] = []
    supplied_keys_by_window: list[set[SegmentKey]] = []
    owner_maps: list[dict[SegmentKey, Owner]] = []
    sessions: dict[Node, Beta8PrimarySession] = {}

    for window_index, window in enumerate(windows):
        if not window.index.input_complete:
            raise EventIndexWindowMergeError(
                f"window {window.name} is incomplete: {window.index.input_error}"
            )
        supplied_keys = {
            _segment_key(segment)
            for segment in window.supplied_transcript
            if segment.get("is_reliable") is not False
        }
        core_keys = tuple(
            _segment_key(segment)
            for segment in window.core_transcript
            if segment.get("is_reliable") is not False
        )
        if not core_keys or not set(core_keys) <= supplied_keys:
            raise EventIndexWindowMergeError(
                f"window {window.name} core is empty or outside supplied context"
            )
        try:
            core_positions = [positions[key] for key in core_keys]
        except KeyError as exc:
            raise EventIndexWindowMergeError(
                f"window {window.name} contains a segment outside the full transcript"
            ) from exc
        if core_positions != list(range(core_positions[0], core_positions[-1] + 1)):
            raise EventIndexWindowMergeError(
                f"window {window.name} core is not globally contiguous"
            )
        core_keys_by_window.append(core_keys)
        supplied_keys_by_window.append(supplied_keys)
        session_by_id = {item.session_id: item for item in window.index.primary_sessions}
        sessions.update({(window_index, key): value for key, value in session_by_id.items()})
        owners: dict[SegmentKey, Owner] = {}
        for coverage in window.index.coverage_ranges:
            keys = _expand_range(coverage.range, files=files, aliases=aliases)
            value = (
                ("session", str(coverage.session_id))
                if coverage.disposition == "session"
                else ("excluded", str(coverage.excluded_reason))
            )
            for key in keys:
                if key in owners:
                    raise EventIndexWindowMergeError(
                        f"window {window.name} overlaps coverage at {key[1]}"
                    )
                owners[key] = value
        missing = supplied_keys - owners.keys()
        if missing:
            first = min(missing, key=positions.__getitem__)
            raise EventIndexWindowMergeError(
                f"window {window.name} has a coverage gap at {first[1]}"
            )
        for owner in owners.values():
            if owner[0] == "session" and owner[1] not in session_by_id:
                raise EventIndexWindowMergeError(
                    f"window {window.name} references unknown session {owner[1]}"
                )
        owner_maps.append(owners)

    flattened_core = [key for group in core_keys_by_window for key in group]
    if flattened_core != full_keys:
        raise EventIndexWindowMergeError(
            "window cores must cover the full reliable transcript exactly once in order"
        )

    parent: dict[Node, Node] = {node: node for node in sessions}

    def find(node: Node) -> Node:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: Node, right: Node) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for boundary in range(len(windows) - 1):
        left_key = core_keys_by_window[boundary][-1]
        right_key = core_keys_by_window[boundary + 1][0]
        if not {
            left_key,
            right_key,
        } <= supplied_keys_by_window[boundary] or not {
            left_key,
            right_key,
        } <= supplied_keys_by_window[boundary + 1]:
            raise EventIndexWindowMergeError(
                f"adjacent windows lack shared boundary context after {windows[boundary].name}"
            )
        left_view = owner_maps[boundary]
        right_view = owner_maps[boundary + 1]
        left_continues = left_view[left_key] == left_view[right_key]
        right_continues = right_view[left_key] == right_view[right_key]
        if left_continues != right_continues:
            raise EventIndexWindowMergeError(
                f"continuity disagreement after window {windows[boundary].name}"
            )
        if not left_continues:
            continue
        left_owner = left_view[left_key]
        right_owner = right_view[right_key]
        if left_owner[0] != right_owner[0]:
            raise EventIndexWindowMergeError(
                f"boundary owner type disagreement after {windows[boundary].name}"
            )
        if left_owner[0] == "excluded":
            if left_owner[1] != right_owner[1]:
                raise EventIndexWindowMergeError(
                    f"boundary exclusion disagreement after {windows[boundary].name}"
                )
            continue
        left_node = (boundary, left_owner[1])
        right_node = (boundary + 1, right_owner[1])
        if _session_signature(sessions[left_node]) != _session_signature(sessions[right_node]):
            raise EventIndexWindowMergeError(
                f"boundary session type disagreement after {windows[boundary].name}"
            )
        union(left_node, right_node)

    keys_by_root: dict[Node, list[SegmentKey]] = {}
    nodes_by_root: dict[Node, set[Node]] = {}
    core_keys_by_node: dict[Node, list[SegmentKey]] = {}
    core_owner_by_key: dict[SegmentKey, tuple[str, Node | str]] = {}
    for window_index, core_keys in enumerate(core_keys_by_window):
        for key in core_keys:
            owner = owner_maps[window_index][key]
            if owner[0] == "session":
                node = (window_index, owner[1])
                root = find(node)
                keys_by_root.setdefault(root, []).append(key)
                nodes_by_root.setdefault(root, set()).add(node)
                core_keys_by_node.setdefault(node, []).append(key)
                core_owner_by_key[key] = ("session", root)
            else:
                core_owner_by_key[key] = ("excluded", owner[1])

    ordered_roots = sorted(
        keys_by_root,
        key=lambda root: min(positions[key] for key in keys_by_root[root]),
    )
    global_id_by_root = {
        root: f"session_{number:03d}"
        for number, root in enumerate(ordered_roots, start=1)
    }
    primary_sessions: list[Beta8PrimarySession] = []
    for root in ordered_roots:
        owned = sorted(keys_by_root[root], key=positions.__getitem__)
        nodes = sorted(
            nodes_by_root[root],
            key=lambda node: min(positions[key] for key in core_keys_by_node[node]),
        )
        start_session = sessions[nodes[0]]
        end_session = sessions[nodes[-1]]
        start_boundary = start_session.start_boundary
        end_boundary = end_session.end_boundary
        if owned[0] != full_keys[0] and start_boundary == "input_start":
            start_boundary = "activity_started"
        if owned[-1] != full_keys[-1] and end_boundary == "input_end":
            end_boundary = "activity_ended"
        primary_sessions.append(Beta8PrimarySession(
            session_id=global_id_by_root[root],
            activity_kind=start_session.activity_kind,
            communication_purpose=_merge_communication_purpose(
                [sessions[node] for node in nodes]
            ),
            participation_mode=start_session.participation_mode,
            start_boundary=start_boundary,
            start_boundary_evidence_segment_ids=[owned[0][1]],
            end_boundary=end_boundary,
            end_boundary_evidence_segment_ids=[owned[-1][1]],
            subject=_merge_text([sessions[node].subject for node in nodes], 120),
            description=_merge_text(
                [sessions[node].description for node in nodes], 300
            ),
            ranges=_runs(owned, positions=positions),
        ))

    embedded_events: list[Beta8EmbeddedEventDraft] = []
    for window_index, window in enumerate(windows):
        core_set = set(core_keys_by_window[window_index])
        for event in window.index.embedded_events:
            evidence = [
                key
                for segment_range in event.evidence_ranges
                for key in _expand_range(segment_range, files=files, aliases=aliases)
                if key in core_set
            ]
            if not evidence:
                continue
            parent_node = (window_index, event.parent_session_id)
            if parent_node not in parent:
                raise EventIndexWindowMergeError(
                    f"embedded event {event.event_id} has an unknown parent"
                )
            root = find(parent_node)
            global_parent = global_id_by_root.get(root)
            if global_parent is None:
                raise EventIndexWindowMergeError(
                    f"embedded event {event.event_id} has evidence outside parent core"
                )
            embedded_events.append(Beta8EmbeddedEventDraft(
                event_id=f"event_{len(embedded_events) + 1:03d}",
                parent_session_id=global_parent,
                event_kind=event.event_kind,
                expression_mode=event.expression_mode,
                subject=event.subject,
                description=event.description,
                evidence_ranges=_runs(evidence, positions=positions),
            ))

    coverage_ranges: list[Beta8CoverageRange] = []
    run_start = full_keys[0]
    previous = full_keys[0]

    def global_owner(key: SegmentKey) -> Owner:
        kind, value = core_owner_by_key[key]
        if kind == "session":
            return kind, global_id_by_root[value]  # type: ignore[index]
        return kind, str(value)

    run_owner = global_owner(run_start)

    def finish_coverage() -> None:
        segment_range = Beta8SegmentRange(
            source_file=run_start[0],
            start_segment_id=run_start[1],
            end_segment_id=previous[1],
        )
        if run_owner[0] == "session":
            coverage_ranges.append(Beta8CoverageRange(
                range=segment_range,
                disposition="session",
                session_id=run_owner[1],
                origin="model",
            ))
        else:
            coverage_ranges.append(Beta8CoverageRange(
                range=segment_range,
                disposition="excluded",
                excluded_reason=run_owner[1],  # type: ignore[arg-type]
                origin="model",
            ))

    for current in full_keys[1:]:
        owner = global_owner(current)
        contiguous = (
            current[0] == previous[0]
            and positions[current] == positions[previous] + 1
        )
        if owner != run_owner or not contiguous:
            finish_coverage()
            run_start = current
            run_owner = owner
        previous = current
    finish_coverage()

    return Beta8NormalizedEventIndex(
        input_complete=True,
        input_error=None,
        primary_sessions=primary_sessions,
        embedded_events=embedded_events,
        coverage_ranges=coverage_ranges,
        system_excluded_ranges=_system_excluded_ranges(full_transcript),
    )
