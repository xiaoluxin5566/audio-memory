from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


class EvidenceError(ValueError):
    """A stage result cannot be tied safely to the material it received."""


P1 = {"status", "window_id", "activities", "topics", "commitments", "context_requests"}
ACTIVITY = {"local_key", "start_segment_id", "end_segment_id", "activity_kind", "participation", "purpose", "subject", "continuation_note"}
TOPIC = {"local_key", "parent_activity_key", "ranges", "provenance", "understanding", "open_questions", "evidence_anchors"}
COMMITMENT = {"local_key", "text", "actor", "acceptance_state", "time_text", "evidence_segment_id"}
PLAN = {"status", "cards", "topic_dispositions", "research_tasks", "accepted_todo_keys", "context_requests", "budget_gaps"}
BRIEF = {"draft_key", "scene_id", "source_activity_ids", "topic_ids", "related_topic_ids", "reader_need", "must_answer", "must_keep", "useful_deliverable", "research_decision"}
CARD = {"status", "card_id", "markdown", "topic_dispositions", "evidence_anchors", "new_topics", "new_commitments", "requests"}


def obj(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be an object")
    return value


def arr(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be an array")
    return value


def fields(value: Mapping[str, object], required: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise EvidenceError(f"{label} missing fields: {', '.join(missing)}")


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} must be a non-empty string")
    return value.strip()


def nullable_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceError(f"{label} must be a string or null")
    return value


def ids(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    values = arr(value, label)
    result = [text(item, f"{label}[{index}]") for index, item in enumerate(values)]
    if nonempty and not result:
        raise EvidenceError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise EvidenceError(f"{label} contains duplicate IDs")
    return result


class EvidenceCatalog:
    """Dictionary interfaces and deterministic structural guards for P1/P2/P4."""

    def __init__(self, segments: Sequence[Mapping[str, object]]) -> None:
        if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence):
            raise EvidenceError("segments must be an array")
        self._segments: list[dict[str, object]] = []
        seen: set[str] = set()
        for index, raw in enumerate(segments):
            segment = obj(raw, f"segments[{index}]")
            fields(segment, {"segment_id", "source_file", "text"}, f"segments[{index}]")
            segment_id = text(segment["segment_id"], f"segments[{index}].segment_id")
            text(segment["source_file"], f"segments[{index}].source_file")
            if not isinstance(segment["text"], str):
                raise EvidenceError(f"segments[{index}].text must be a string")
            if segment_id in seen:
                raise EvidenceError(f"duplicate segment_id: {segment_id}")
            seen.add(segment_id)
            if segment.get("is_reliable") is not False:
                self._segments.append(deepcopy(dict(segment)))
        if not self._segments:
            raise EvidenceError("at least one reliable segment is required")
        self._by_id = {str(row["segment_id"]): row for row in self._segments}
        self._global = {str(row["segment_id"]): index for index, row in enumerate(self._segments)}
        self._source = {str(row["segment_id"]): str(row["source_file"]) for row in self._segments}
        self._file_ids: dict[str, list[str]] = {}
        for row in self._segments:
            self._file_ids.setdefault(str(row["source_file"]), []).append(str(row["segment_id"]))
        self._file_pos = {source: {item: index for index, item in enumerate(values)} for source, values in self._file_ids.items()}

    def windows(self, core_budget_bytes: int, boundary_segments: int = 2) -> list[dict[str, object]]:
        if isinstance(core_budget_bytes, bool) or not isinstance(core_budget_bytes, int) or core_budget_bytes <= 0:
            raise EvidenceError("core_budget_bytes must be a positive integer")
        if isinstance(boundary_segments, bool) or not isinstance(boundary_segments, int) or boundary_segments < 0:
            raise EvidenceError("boundary_segments must be a non-negative integer")
        groups: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        used = 0
        source: str | None = None
        for segment in self._segments:
            size = len(str(segment["text"]).encode("utf-8"))
            changed_file = bool(current) and str(segment["source_file"]) != source
            if current and (changed_file or used + size > core_budget_bytes):
                groups.append(current)
                current, used = [], 0
            current.append(deepcopy(segment))  # one oversize segment remains intact
            used += size
            source = str(segment["source_file"])
        if current:
            groups.append(current)
        result: list[dict[str, object]] = []
        for number, core in enumerate(groups, 1):
            source = str(core[0]["source_file"])
            values = self._file_ids[source]
            start = self._file_pos[source][str(core[0]["segment_id"])]
            end = self._file_pos[source][str(core[-1]["segment_id"])]
            before = values[max(0, start - boundary_segments):start]
            after = values[end + 1:end + 1 + boundary_segments]
            result.append({
                "window_id": f"window_{number:03d}",
                "core_segments": core,
                "boundary_context": {
                    "before": [deepcopy(self._by_id[item]) for item in before],
                    "after": [deepcopy(self._by_id[item]) for item in after],
                },
            })
        return result

    def _range(self, value: object, label: str, allowed: set[str] | None = None) -> list[str]:
        source_range = obj(value, label)
        fields(source_range, {"source_file", "start_segment_id", "end_segment_id"}, label)
        source = text(source_range["source_file"], f"{label}.source_file")
        start = text(source_range["start_segment_id"], f"{label}.start_segment_id")
        end = text(source_range["end_segment_id"], f"{label}.end_segment_id")
        return self._bounds(start, end, label, allowed, declared_source=source)[1]

    def _bounds(self, start: object, end: object, label: str, allowed: set[str] | None = None, *, declared_source: str | None = None) -> tuple[str, list[str]]:
        first, last = text(start, f"{label}.start_segment_id"), text(end, f"{label}.end_segment_id")
        first_source, last_source = self._source.get(first), self._source.get(last)
        if first_source is None:
            raise EvidenceError(f"{label} references unknown segment: {first}")
        if last_source is None:
            raise EvidenceError(f"{label} references unknown segment: {last}")
        if first_source != last_source:
            raise EvidenceError(f"{label} is a cross-file range")
        if declared_source is not None and declared_source != first_source:
            kind = "unknown" if declared_source not in self._file_ids else "wrong"
            raise EvidenceError(f"{label} has {kind} source_file: {declared_source}")
        positions = self._file_pos[first_source]
        if positions[first] > positions[last]:
            raise EvidenceError(f"{label} is a reversed range")
        result = self._file_ids[first_source][positions[first]:positions[last] + 1]
        if allowed is not None and not set(result) <= allowed:
            raise EvidenceError(f"{label} extends outside supplied material")
        return first_source, list(result)

    def _runs(self, values: Sequence[str]) -> list[dict[str, str]]:
        ordered = sorted(set(values), key=self._global.__getitem__)
        if not ordered:
            return []
        result: list[dict[str, str]] = []
        start = previous = ordered[0]
        for current in ordered[1:]:
            contiguous = self._source[current] == self._source[previous] and self._file_pos[self._source[current]][current] == self._file_pos[self._source[previous]][previous] + 1
            if not contiguous:
                result.append({"source_file": self._source[start], "start_segment_id": start, "end_segment_id": previous})
                start = current
            previous = current
        result.append({"source_file": self._source[start], "start_segment_id": start, "end_segment_id": previous})
        return result

    def _windows(
        self,
        windows: Sequence[Mapping[str, object]],
        *,
        require_complete_coverage: bool = True,
    ) -> list[dict[str, Any]]:
        if isinstance(windows, (str, bytes)) or not isinstance(windows, Sequence):
            raise EvidenceError("windows must be an array")
        result: list[dict[str, Any]] = []
        flattened: list[str] = []
        seen: set[str] = set()
        for index, raw in enumerate(windows):
            window = obj(raw, f"windows[{index}]")
            fields(window, {"window_id", "core_segments", "boundary_context"}, f"windows[{index}]")
            window_id = text(window["window_id"], f"windows[{index}].window_id")
            if window_id in seen:
                raise EvidenceError(f"duplicate window_id: {window_id}")
            seen.add(window_id)
            boundary = obj(window["boundary_context"], f"{window_id}.boundary_context")
            fields(boundary, {"before", "after"}, f"{window_id}.boundary_context")
            parsed: dict[str, list[dict[str, object]]] = {}
            for name, raw_rows in (("before", boundary["before"]), ("core", window["core_segments"]), ("after", boundary["after"])):
                parsed[name] = []
                for row_index, row in enumerate(arr(raw_rows, f"{window_id}.{name}")):
                    segment_id = text(obj(row, f"{window_id}.{name}[{row_index}]").get("segment_id"), f"{window_id}.{name}[{row_index}].segment_id")
                    if segment_id not in self._by_id:
                        raise EvidenceError(f"window {window_id} references unknown segment: {segment_id}")
                    parsed[name].append(deepcopy(self._by_id[segment_id]))
                    if name == "core":
                        flattened.append(segment_id)
            if not parsed["core"]:
                raise EvidenceError(f"window {window_id} core must not be empty")
            supplied_ids = [str(row["segment_id"]) for name in ("before", "core", "after") for row in parsed[name]]
            if len(supplied_ids) != len(set(supplied_ids)):
                raise EvidenceError(f"window {window_id} repeats supplied segments")
            result.append({"window_id": window_id, **parsed, "core_ids": [str(row["segment_id"]) for row in parsed["core"]], "supplied_ids": supplied_ids})
        if require_complete_coverage and flattened != [
            str(row["segment_id"]) for row in self._segments
        ]:
            raise EvidenceError("window cores must own every reliable segment exactly once in order")
        return result

    def _parse_window(self, result: Mapping[str, object], window: Mapping[str, Any], window_index: int) -> dict[str, Any]:
        window_id = str(window["window_id"])
        fields(result, P1, f"P1 {window_id}")
        if result["window_id"] != window_id:
            raise EvidenceError(f"P1 window_id mismatch: expected {window_id}")
        if result["status"] not in {"complete", "needs_context"}:
            raise EvidenceError(f"invalid P1 status for {window_id}: {result['status']}")
        requests = arr(result["context_requests"], f"{window_id}.context_requests")
        if result["status"] == "complete" and requests:
            raise EvidenceError(f"complete P1 result {window_id} cannot have context_requests")
        if result["status"] == "needs_context" and not requests:
            raise EvidenceError(f"needs_context P1 result {window_id} requires context_requests")
        raw_activities = arr(result["activities"], f"{window_id}.activities")
        raw_topics = arr(result["topics"], f"{window_id}.topics")
        if result["status"] == "complete" and not raw_topics:
            raise EvidenceError(f"complete P1 result {window_id} cannot use empty topics")
        core, supplied = set(window["core_ids"]), set(window["supplied_ids"])
        local_keys: set[str] = set()
        activities: list[dict[str, Any]] = []
        activity_by_key: dict[str, dict[str, Any]] = {}
        owners: dict[str, str] = {}
        for index, raw in enumerate(raw_activities):
            activity = obj(raw, f"{window_id}.activities[{index}]")
            fields(activity, ACTIVITY, f"{window_id}.activities[{index}]")
            key = text(activity["local_key"], f"{window_id}.activities[{index}].local_key")
            if key in local_keys:
                raise EvidenceError(f"duplicate local_key in {window_id}: {key}")
            local_keys.add(key)
            for name in ("activity_kind", "participation", "purpose", "subject"):
                text(activity[name], f"{window_id}.{key}.{name}")
            nullable_text(activity["continuation_note"], f"{window_id}.{key}.continuation_note")
            source, full = self._bounds(activity["start_segment_id"], activity["end_segment_id"], f"{window_id}.{key}", supplied)
            owned = [item for item in full if item in core]
            if not owned:
                raise EvidenceError(f"activity {window_id}/{key} owns no core segment")
            for segment_id in owned:
                if segment_id in owners:
                    raise EvidenceError(f"activities overlap core segment {segment_id} in {window_id}")
                owners[segment_id] = key
            part = {"window_index": window_index, "window_id": window_id, "local_key": key, "raw": deepcopy(dict(activity)), "source_file": source, "full_ids": full, "core_ids": owned}
            activities.append(part)
            activity_by_key[key] = part
        missing = [item for item in window["core_ids"] if item not in owners]
        if missing:
            raise EvidenceError(f"P1 activities leave core segment uncovered in {window_id}: {missing[0]}")
        topics: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_topics):
            topic = obj(raw, f"{window_id}.topics[{index}]")
            fields(topic, TOPIC, f"{window_id}.topics[{index}]")
            key = text(topic["local_key"], f"{window_id}.topics[{index}].local_key")
            if key in local_keys:
                raise EvidenceError(f"duplicate local_key in {window_id}: {key}")
            local_keys.add(key)
            parent_key = text(topic["parent_activity_key"], f"{window_id}.{key}.parent_activity_key")
            parent = activity_by_key.get(parent_key)
            if parent is None:
                raise EvidenceError(f"topic {window_id}/{key} has unknown parent activity")
            for name in ("provenance", "understanding"):
                text(topic[name], f"{window_id}.{key}.{name}")
            questions = topic["open_questions"]
            questions = [] if questions is None else [questions] if isinstance(questions, str) else arr(questions, f"{window_id}.{key}.open_questions")
            topic_ranges = arr(topic["ranges"], f"{window_id}.{key}.ranges")
            if not topic_ranges:
                raise EvidenceError(f"topic {window_id}/{key} must have ranges")
            full = [item for range_index, source_range in enumerate(topic_ranges) for item in self._range(source_range, f"{window_id}.{key}.ranges[{range_index}]", supplied)]
            if not set(full) & core:
                raise EvidenceError(f"topic {window_id}/{key} is boundary-only")
            if not set(full) <= set(parent["full_ids"]):
                raise EvidenceError(f"topic {window_id}/{key} is outside its parent activity")
            canonical_anchors = []
            for anchor_index, raw_anchor in enumerate(arr(topic["evidence_anchors"], f"{window_id}.{key}.evidence_anchors")):
                anchor = {"segment_id": raw_anchor} if isinstance(raw_anchor, str) else obj(raw_anchor, f"{window_id}.{key}.evidence_anchors[{anchor_index}]")
                canonical_anchors.append(deepcopy(dict(anchor)))
                expected_anchor_fields = {"segment_id", "quote"} if "quote" in anchor else {"segment_id"}
                fields(anchor, expected_anchor_fields, f"{window_id}.{key}.evidence_anchors[{anchor_index}]")
                segment_id = text(anchor["segment_id"], f"{window_id}.{key}.anchor.segment_id")
                if segment_id not in full:
                    raise EvidenceError(f"topic anchor {segment_id} is outside topic ranges")
                # New P1 outputs identify evidence; the program copies verbatim text.
                # Legacy outputs that supply a quote remain strictly checked.
                if "quote" in anchor:
                    quote = text(anchor["quote"], f"{window_id}.{key}.anchor.quote")
                    if quote not in str(self._by_id[segment_id]["text"]):
                        raise EvidenceError(f"topic anchor quote is not exact for {segment_id}")
            topics.append({"window_index": window_index, "window_id": window_id, "local_key": key, "parent": parent, "raw": dict(deepcopy(dict(topic)), evidence_anchors=canonical_anchors, open_questions=deepcopy(questions)), "full_ids": list(dict.fromkeys(full)), "core_ids": [item for item in dict.fromkeys(full) if item in core]})
        commitments: list[dict[str, Any]] = []
        for index, raw in enumerate(arr(result["commitments"], f"{window_id}.commitments")):
            commitment = obj(raw, f"{window_id}.commitments[{index}]")
            fields(commitment, COMMITMENT, f"{window_id}.commitments[{index}]")
            key = text(commitment["local_key"], f"{window_id}.commitments[{index}].local_key")
            if key in local_keys:
                raise EvidenceError(f"duplicate local_key in {window_id}: {key}")
            local_keys.add(key)
            text(commitment["text"], f"{window_id}.{key}.text")
            for name in ("actor", "acceptance_state", "time_text"):
                nullable_text(commitment[name], f"{window_id}.{key}.{name}")
            evidence = text(commitment["evidence_segment_id"], f"{window_id}.{key}.evidence_segment_id")
            if evidence not in core:
                raise EvidenceError(f"commitment {window_id}/{key} evidence is outside its core")
            commitments.append({"window_index": window_index, "window_id": window_id, "raw": deepcopy(dict(commitment)), "evidence": evidence})
        return {"activities": activities, "topics": topics, "commitments": commitments, "requests": deepcopy(requests)}

    def validate_window(self, result: Mapping[str, object], window: Mapping[str, object]) -> dict[str, object]:
        """Validate one P1 response before later windows are dispatched."""
        parsed_windows = self._windows([window], require_complete_coverage=False)
        self._parse_window(obj(result, "result"), parsed_windows[0], 0)
        return deepcopy(dict(result))

    def normalize(self, results: Sequence[Mapping[str, object]], windows: Sequence[Mapping[str, object]]) -> dict[str, object]:
        parsed_windows = self._windows(windows)
        if isinstance(results, (str, bytes)) or not isinstance(results, Sequence):
            raise EvidenceError("results must be an array")
        by_window: dict[str, Mapping[str, object]] = {}
        for index, raw in enumerate(results):
            result = obj(raw, f"results[{index}]")
            window_id = text(result.get("window_id"), f"results[{index}].window_id")
            if window_id in by_window:
                raise EvidenceError(f"duplicate P1 window_id: {window_id}")
            by_window[window_id] = result
        if set(by_window) != {row["window_id"] for row in parsed_windows}:
            raise EvidenceError("P1 results must match every supplied window exactly once")
        parsed = [self._parse_window(by_window[row["window_id"]], row, index) for index, row in enumerate(parsed_windows)]
        parts = [part for item in parsed for part in item["activities"]]
        topics = [topic for item in parsed for topic in item["topics"]]
        commitments = [commitment for item in parsed for commitment in item["commitments"]]
        parent = list(range(len(parts)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        part_index = {id(part): index for index, part in enumerate(parts)}
        raw_boundaries: list[dict[str, Any]] = []
        for boundary in range(len(parsed_windows) - 1):
            left_window, right_window = parsed_windows[boundary], parsed_windows[boundary + 1]
            left_id, right_id = left_window["core_ids"][-1], right_window["core_ids"][0]
            left_parts = [part for part in parts if part["window_index"] == boundary and left_id in part["core_ids"]]
            right_parts = [part for part in parts if part["window_index"] == boundary + 1 and right_id in part["core_ids"]]
            reciprocal = [
                (left, right)
                for left in left_parts
                for right in right_parts
                if right_id in left["full_ids"] and left_id in right["full_ids"]
            ]
            compatible = [
                (left, right)
                for left, right in reciprocal
                if all(
                    left["raw"].get(name) == right["raw"].get(name)
                    for name in ("activity_kind", "participation", "purpose")
                )
            ]
            left = left_parts[0] if len(left_parts) == 1 else None
            right = right_parts[0] if len(right_parts) == 1 else None
            if len(compatible) == 1 and len(reciprocal) == 1:
                left, right = compatible[0]
                left_root, right_root = find(part_index[id(left)]), find(part_index[id(right)])
                if left_root != right_root:
                    parent[right_root] = left_root
                state = "linked"
            else:
                has_note = any(part and part["raw"].get("continuation_note") for part in (left, right))
                state = "ambiguous" if reciprocal or has_note else "separate"
            raw_boundaries.append({"boundary_id": f"boundary_{boundary + 1:03d}", "left_window_id": left_window["window_id"], "right_window_id": right_window["window_id"], "state": state, "left": left, "right": right, "evidence_segment_ids": [left_id, right_id]})
        components: dict[int, list[dict[str, Any]]] = {}
        for index, part in enumerate(parts):
            components.setdefault(find(index), []).append(part)
        components_in_order = sorted(components.values(), key=lambda group: min(self._global[item] for part in group for item in part["core_ids"]))
        activity_ids: dict[int, str] = {}
        activities: list[dict[str, object]] = []
        for number, group in enumerate(components_in_order, 1):
            group.sort(key=lambda part: part["window_index"])
            activity_id = f"activity_{number:03d}"
            segment_ids = sorted({item for part in group for item in part["core_ids"]}, key=self._global.__getitem__)
            first = group[0]["raw"]
            normalized = {key: deepcopy(value) for key, value in first.items() if key not in {"local_key", "start_segment_id", "end_segment_id"}}
            normalized.update({"activity_id": activity_id, "source_ranges": self._runs(segment_ids), "window_parts": [{**deepcopy(part["raw"]), "window_id": part["window_id"], "source_file": part["source_file"], "reported_range": {"source_file": part["source_file"], "start_segment_id": part["full_ids"][0], "end_segment_id": part["full_ids"][-1]}, "core_ranges": self._runs(part["core_ids"])} for part in group]})
            activities.append(normalized)
            for part in group:
                activity_ids[id(part)] = activity_id
        topics.sort(key=lambda topic: (min(self._global[item] for item in topic["core_ids"]), topic["window_index"]))
        registered_topics = []
        for number, topic in enumerate(topics, 1):
            normalized = deepcopy(topic["raw"])
            normalized["evidence_anchors"] = [
                {"segment_id": anchor["segment_id"],
                 "quote": anchor["quote"] if "quote" in anchor else str(self._by_id[anchor["segment_id"]]["text"])}
                for anchor in normalized["evidence_anchors"]
            ]
            normalized.update({"topic_id": f"topic_{number:03d}", "parent_activity_id": activity_ids[id(topic["parent"])], "window_id": topic["window_id"], "core_ranges": self._runs(topic["core_ids"])})
            registered_topics.append(normalized)
        commitments.sort(key=lambda item: (self._global[item["evidence"]], item["window_index"]))
        normalized_commitments = []
        for number, commitment in enumerate(commitments, 1):
            normalized = deepcopy(commitment["raw"])
            normalized.update({"commitment_id": f"commitment_{number:03d}", "window_id": commitment["window_id"]})
            normalized_commitments.append(normalized)
        boundary_evidence = []
        for item in raw_boundaries:
            left, right = item.pop("left"), item.pop("right")
            boundary_evidence.append({**item, "left_activity_id": activity_ids.get(id(left)) if left else None, "right_activity_id": activity_ids.get(id(right)) if right else None, "continuation_notes": [part["raw"]["continuation_note"] for part in (left, right) if part and part["raw"].get("continuation_note")]})
        for number, item in enumerate(parsed, 1):
            if item["requests"]:
                boundary_evidence.append({"boundary_id": f"context_{number:03d}", "window_id": parsed_windows[number - 1]["window_id"], "state": "needs_context", "context_requests": item["requests"]})
        return {"normalized_activities": activities, "registered_topics": registered_topics, "commitments": normalized_commitments, "boundary_evidence": boundary_evidence}

    def _registry(self, registry: Mapping[str, object]) -> tuple[dict[str, Mapping[str, object]], dict[str, Mapping[str, object]], dict[str, Mapping[str, object]]]:
        registry = obj(registry, "registry")
        fields(registry, {"normalized_activities", "registered_topics", "commitments", "boundary_evidence"}, "registry")

        def index(value: object, key: str, label: str) -> dict[str, Mapping[str, object]]:
            result: dict[str, Mapping[str, object]] = {}
            for number, raw in enumerate(arr(value, label)):
                row = obj(raw, f"{label}[{number}]")
                identifier = text(row.get(key), f"{label}[{number}].{key}")
                if identifier in result:
                    raise EvidenceError(f"duplicate {key}: {identifier}")
                result[identifier] = row
            return result
        return index(registry["normalized_activities"], "activity_id", "normalized_activities"), index(registry["registered_topics"], "topic_id", "registered_topics"), index(registry["commitments"], "commitment_id", "commitments")

    def _brief(self, brief: Mapping[str, object], activities: Mapping[str, Mapping[str, object]], topics: Mapping[str, Mapping[str, object]], label: str) -> dict[str, Any]:
        fields(brief, BRIEF, label)
        result = {"draft_key": text(brief["draft_key"], f"{label}.draft_key"), "scene_id": text(brief["scene_id"], f"{label}.scene_id"), "activities": ids(brief["source_activity_ids"], f"{label}.source_activity_ids", nonempty=True), "topics": ids(brief["topic_ids"], f"{label}.topic_ids"), "related": ids(brief["related_topic_ids"], f"{label}.related_topic_ids")}
        for name in ("reader_need", "useful_deliverable"):
            text(brief[name], f"{label}.{name}")
        arr(brief["must_answer"], f"{label}.must_answer")
        arr(brief["must_keep"], f"{label}.must_keep")
        if set(result["topics"]) & set(result["related"]):
            raise EvidenceError(f"{label} repeats primary and related topics")
        unknown = set(result["activities"]) - activities.keys()
        if unknown:
            raise EvidenceError(f"{label} references unknown activity: {sorted(unknown)[0]}")
        unknown = set(result["topics"] + result["related"]) - topics.keys()
        if unknown:
            raise EvidenceError(f"{label} references unknown topic: {sorted(unknown)[0]}")
        for topic_id in result["topics"]:
            if topics[topic_id].get("parent_activity_id") not in result["activities"]:
                raise EvidenceError(f"{label} primary topic {topic_id} is outside source activities")
        research = obj(brief["research_decision"], f"{label}.research_decision")
        fields(research, {"decision", "reason", "task_keys"}, f"{label}.research_decision")
        if research["decision"] not in {"search", "no_search"}:
            raise EvidenceError(f"{label}.research_decision must be explicit search or no_search")
        text(research["reason"], f"{label}.research_decision.reason")
        result["task_keys"] = ids(research["task_keys"], f"{label}.research_decision.task_keys")
        if (research["decision"] == "search") != bool(result["task_keys"]):
            raise EvidenceError(f"{label}.research_decision conflicts with task_keys")
        return result

    def validate_plan(self, plan: Mapping[str, object], registry: Mapping[str, object]) -> dict[str, object]:
        plan = obj(plan, "plan")
        fields(plan, PLAN, "plan")
        if plan["status"] not in {"complete", "needs_context"}:
            raise EvidenceError("plan.status must be complete or needs_context")
        context_requests = arr(plan["context_requests"], "plan.context_requests")
        if plan["status"] == "complete" and context_requests:
            raise EvidenceError("complete plan cannot have context_requests")
        activities, topics, commitments = self._registry(registry)
        cards: dict[str, dict[str, Any]] = {}
        for number, raw in enumerate(arr(plan["cards"], "plan.cards")):
            card = obj(raw, f"plan.cards[{number}]")
            parsed = self._brief(card, activities, topics, f"plan.cards[{number}]")
            if parsed["draft_key"] in cards:
                raise EvidenceError(f"duplicate draft_key: {parsed['draft_key']}")
            cards[parsed["draft_key"]] = parsed
        dispositions: dict[str, tuple[str, list[str]]] = {}
        for number, raw in enumerate(arr(plan["topic_dispositions"], "plan.topic_dispositions")):
            disposition = obj(raw, f"plan.topic_dispositions[{number}]")
            fields(disposition, {"topic_id", "state", "card_keys", "reason"}, f"plan.topic_dispositions[{number}]")
            topic_id = text(disposition["topic_id"], f"plan.topic_dispositions[{number}].topic_id")
            if topic_id not in topics or topic_id in dispositions:
                raise EvidenceError(f"unknown or duplicate topic disposition: {topic_id}")
            state = disposition["state"]
            if state not in {"assigned", "excluded", "needs_context"}:
                raise EvidenceError(f"invalid topic disposition state: {state}")
            card_keys = ids(disposition["card_keys"], f"plan.topic_dispositions[{number}].card_keys")
            text(disposition["reason"], f"plan.topic_dispositions[{number}].reason")
            if (state == "assigned") != bool(card_keys):
                raise EvidenceError(f"topic disposition {topic_id} conflicts with card_keys")
            if set(card_keys) - cards.keys():
                raise EvidenceError(f"topic disposition {topic_id} references unknown card")
            dispositions[topic_id] = str(state), card_keys
        if set(dispositions) != set(topics):
            raise EvidenceError("every registered topic requires exactly one disposition")
        if plan["status"] == "complete" and any(
            state == "needs_context" for state, _ in dispositions.values()
        ):
            raise EvidenceError("complete plan cannot have needs_context dispositions")
        for card_key, card in cards.items():
            for topic_id in card["topics"]:
                if dispositions[topic_id][0] != "assigned" or card_key not in dispositions[topic_id][1]:
                    raise EvidenceError(f"topic/card references are not symmetric for {topic_id}/{card_key}")
        for topic_id, (state, card_keys) in dispositions.items():
            for card_key in card_keys:
                if topic_id not in cards[card_key]["topics"]:
                    raise EvidenceError(f"topic/card references are not symmetric for {topic_id}/{card_key}")
        if plan["status"] == "complete":
            work_activity_ids = {
                activity_id
                for activity_id, activity in activities.items()
                if activity.get("activity_kind") == "conversation"
                and activity.get("purpose") == "work"
            }
            for activity_id in work_activity_ids:
                primary_cards = [
                    card for card in cards.values() if activity_id in card["activities"]
                ]
                if len(primary_cards) != 1 or primary_cards[0]["scene_id"] != "work_communication":
                    raise EvidenceError(
                        f"canonical work activity {activity_id} requires one primary card"
                    )
            for card in cards.values():
                if len(work_activity_ids & set(card["activities"])) > 1:
                    raise EvidenceError("each canonical work conversation requires one primary card")
        task_fields = {"task_key", "question", "purpose", "public_context", "target_card_keys", "source_requirements", "jurisdiction", "as_of", "version_constraint"}
        tasks: dict[str, list[str]] = {}
        for number, raw in enumerate(arr(plan["research_tasks"], "plan.research_tasks")):
            task = obj(raw, f"plan.research_tasks[{number}]")
            fields(task, task_fields, f"plan.research_tasks[{number}]")
            key = text(task["task_key"], f"plan.research_tasks[{number}].task_key")
            if key in tasks:
                raise EvidenceError(f"duplicate research task_key: {key}")
            for name in ("question", "purpose", "public_context", "source_requirements"):
                text(task[name], f"plan.research_tasks[{number}].{name}")
            for name in ("jurisdiction", "as_of", "version_constraint"):
                nullable_text(task[name], f"plan.research_tasks[{number}].{name}")
            targets = ids(task["target_card_keys"], f"plan.research_tasks[{number}].target_card_keys", nonempty=True)
            if set(targets) - cards.keys():
                raise EvidenceError(f"research task {key} references unknown card")
            tasks[key] = targets
        for card_key, card in cards.items():
            if set(card["task_keys"]) - tasks.keys():
                raise EvidenceError(f"card {card_key} references unknown research task")
            for task_key in card["task_keys"]:
                if card_key not in tasks[task_key]:
                    raise EvidenceError(f"research task/card references are not symmetric for {task_key}/{card_key}")
        for task_key, targets in tasks.items():
            for card_key in targets:
                if task_key not in cards[card_key]["task_keys"]:
                    raise EvidenceError(f"research task/card references are not symmetric for {task_key}/{card_key}")
        accepted = ids(plan["accepted_todo_keys"], "plan.accepted_todo_keys")
        if set(accepted) - commitments.keys():
            raise EvidenceError("accepted_todo_keys references unknown commitment")
        arr(plan["budget_gaps"], "plan.budget_gaps")
        return deepcopy(dict(plan))

    def _source_rows(self, ranges: object, label: str) -> list[dict[str, object]]:
        found = [item for number, source_range in enumerate(arr(ranges, label)) for item in self._range(source_range, f"{label}[{number}]")]
        return [deepcopy(self._by_id[item]) for item in sorted(set(found), key=self._global.__getitem__)]

    def assemble(self, card_id: str, brief: Mapping[str, object], registry: Mapping[str, object], corrections: object) -> dict[str, object]:
        card_id = text(card_id, "card_id")
        brief = obj(brief, "brief")
        activities, topics, _ = self._registry(registry)
        parsed = self._brief(brief, activities, topics, "brief")
        primary: dict[str, dict[str, object]] = {}
        for activity_id in parsed["activities"]:
            for row in self._source_rows(activities[activity_id].get("source_ranges"), f"activity {activity_id}.source_ranges"):
                primary[str(row["segment_id"])] = row
        complete = sorted(primary.values(), key=lambda row: self._global[str(row["segment_id"])])
        related_by_activity: dict[str, list[str]] = {}
        for topic_id in parsed["related"]:
            activity_id = text(topics[topic_id].get("parent_activity_id"), f"topic {topic_id}.parent_activity_id")
            if activity_id not in activities:
                raise EvidenceError(f"topic {topic_id} has unknown parent activity")
            if activity_id not in parsed["activities"]:
                related_by_activity.setdefault(activity_id, []).append(topic_id)
        related_context = []
        for activity_id, topic_ids in related_by_activity.items():
            related_context.append({"activity_id": activity_id, "topic_ids": topic_ids, "activity": deepcopy(dict(activities[activity_id])), "transcript": self._source_rows(activities[activity_id].get("source_ranges"), f"activity {activity_id}.source_ranges")})
        relevant_topics = parsed["topics"] + parsed["related"]
        complete_ids = [str(row["segment_id"]) for row in complete]
        related_ids = [str(row["segment_id"]) for context in related_context for row in context["transcript"]]
        return {"card_id": card_id, "card_brief": deepcopy(dict(brief)), "complete_transcript": complete, "context_scope": {"primary_activity_ids": parsed["activities"], "primary_topic_ids": parsed["topics"], "related_topic_ids": parsed["related"], "related_activity_ids": list(related_by_activity), "complete_transcript_segment_ids": complete_ids, "related_context_segment_ids": related_ids}, "related_context": related_context, "user_corrections": deepcopy(corrections), "topic_registry": [deepcopy(dict(topics[item])) for item in relevant_topics], "research_decision": deepcopy(dict(obj(brief["research_decision"], "brief.research_decision"))), "research_packets": [], "verified_sources": []}

    def _material(self, packet: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
        material: dict[str, Mapping[str, object]] = {}
        def add(value: object, label: str) -> None:
            for number, raw in enumerate(arr(value, label)):
                row = obj(raw, f"{label}[{number}]")
                fields(row, {"segment_id", "source_file", "text"}, f"{label}[{number}]")
                segment_id = text(row["segment_id"], f"{label}[{number}].segment_id")
                if segment_id in material:
                    raise EvidenceError(f"packet repeats supplied segment: {segment_id}")
                material[segment_id] = row
        add(packet.get("complete_transcript"), "packet.complete_transcript")
        for number, raw in enumerate(arr(packet.get("related_context"), "packet.related_context")):
            add(obj(raw, f"packet.related_context[{number}]").get("transcript"), f"packet.related_context[{number}].transcript")
        return material

    @staticmethod
    def _locator(markdown: str, value: object, label: str) -> None:
        locator = text(value, label)
        if locator not in markdown:
            raise EvidenceError(f"{label} does not identify actual markdown content")

    def validate_card(self, result: Mapping[str, object], packet: Mapping[str, object]) -> dict[str, object]:
        result, packet = obj(result, "result"), obj(packet, "packet")
        fields(result, CARD, "result")
        expected = text(packet.get("card_id"), "packet.card_id")
        if text(result["card_id"], "result.card_id") != expected:
            raise EvidenceError("result.card_id does not match packet.card_id")
        status = result["status"]
        if status not in {"complete", "needs_context", "needs_research", "blocked"}:
            raise EvidenceError(f"invalid P4 status: {status}")
        if not isinstance(result["markdown"], str):
            raise EvidenceError("result.markdown must be a string")
        markdown = str(result["markdown"])
        requests = arr(result["requests"], "result.requests")
        nonempty_lines = [line.strip() for line in markdown.splitlines() if line.strip()]
        if status == "complete" and requests:
            raise EvidenceError("complete card cannot have unresolved requests")
        if status == "complete" and (len(nonempty_lines) < 2 or not nonempty_lines[0].startswith("# ")):
            raise EvidenceError("complete card markdown requires an H1 and a core summary")
        if status != "complete" and not requests:
            raise EvidenceError(f"{status} card requires at least one request")
        material = self._material(packet)
        material_ids = set(material)
        allowed_topics = {text(obj(row, "packet topic").get("topic_id"), "packet topic.topic_id") for row in arr(packet.get("topic_registry"), "packet.topic_registry")}
        context_scope = obj(packet.get("context_scope"), "packet.context_scope")
        required_topics = set(
            ids(context_scope.get("primary_topic_ids"), "packet.context_scope.primary_topic_ids")
        )
        if not required_topics <= allowed_topics:
            raise EvidenceError("packet primary topics are absent from topic_registry")
        seen: set[str] = set()
        for number, raw in enumerate(arr(result["topic_dispositions"], "result.topic_dispositions")):
            disposition = obj(raw, f"result.topic_dispositions[{number}]")
            fields(disposition, {"topic_id", "state", "body_locator", "reason"}, f"result.topic_dispositions[{number}]")
            topic_id = text(disposition["topic_id"], f"result.topic_dispositions[{number}].topic_id")
            if topic_id not in allowed_topics or topic_id in seen:
                raise EvidenceError(f"unknown or duplicate card topic disposition: {topic_id}")
            seen.add(topic_id)
            if disposition["state"] == "included":
                self._locator(markdown, disposition["body_locator"], f"result.topic_dispositions[{number}].body_locator")
                nullable_text(disposition["reason"], f"result.topic_dispositions[{number}].reason")
            elif disposition["state"] == "excluded":
                nullable_text(disposition["body_locator"], f"result.topic_dispositions[{number}].body_locator")
                text(disposition["reason"], f"result.topic_dispositions[{number}].reason")
            else:
                raise EvidenceError(f"invalid card topic disposition state: {disposition['state']}")
        if not required_topics <= seen:
            raise EvidenceError("card is missing primary topic disposition")
        for number, raw in enumerate(arr(result["evidence_anchors"], "result.evidence_anchors")):
            anchor = obj(raw, f"result.evidence_anchors[{number}]")
            fields(anchor, {"body_locator", "segment_id", "quote"}, f"result.evidence_anchors[{number}]")
            self._locator(markdown, anchor["body_locator"], f"result.evidence_anchors[{number}].body_locator")
            segment_id = text(anchor["segment_id"], f"result.evidence_anchors[{number}].segment_id")
            if segment_id not in material:
                raise EvidenceError(f"evidence anchor {segment_id} is outside supplied material")
            if anchor["quote"] is not None:
                quote = text(anchor["quote"], f"result.evidence_anchors[{number}].quote")
                if quote not in str(material[segment_id]["text"]):
                    raise EvidenceError(f"evidence anchor quote is not exact for {segment_id}")
        new_keys: set[str] = set()
        for number, raw in enumerate(arr(result["new_topics"], "result.new_topics")):
            topic = obj(raw, f"result.new_topics[{number}]")
            fields(topic, {"local_key", "source_ranges", "description", "body_locator", "suggested_scene"}, f"result.new_topics[{number}]")
            key = text(topic["local_key"], f"result.new_topics[{number}].local_key")
            if key in new_keys:
                raise EvidenceError(f"duplicate new topic local_key: {key}")
            new_keys.add(key)
            ranges = arr(topic["source_ranges"], f"result.new_topics[{number}].source_ranges")
            if not ranges:
                raise EvidenceError("new topic source_ranges must not be empty")
            for range_number, source_range in enumerate(ranges):
                self._range(source_range, f"result.new_topics[{number}].source_ranges[{range_number}]", material_ids)
            text(topic["description"], f"result.new_topics[{number}].description")
            self._locator(markdown, topic["body_locator"], f"result.new_topics[{number}].body_locator")
            nullable_text(topic["suggested_scene"], f"result.new_topics[{number}].suggested_scene")
        for number, raw in enumerate(arr(result["new_commitments"], "result.new_commitments")):
            commitment = obj(raw, f"result.new_commitments[{number}]")
            fields(commitment, {"text", "actor", "acceptance_state", "time_text", "evidence_segment_id"}, f"result.new_commitments[{number}]")
            text(commitment["text"], f"result.new_commitments[{number}].text")
            for name in ("actor", "acceptance_state", "time_text"):
                nullable_text(commitment[name], f"result.new_commitments[{number}].{name}")
            if text(commitment["evidence_segment_id"], f"result.new_commitments[{number}].evidence_segment_id") not in material:
                raise EvidenceError("new commitment evidence is outside supplied material")
        for number, raw in enumerate(requests):
            request = obj(raw, f"result.requests[{number}]")
            fields(request, {"kind", "question", "purpose", "known_source_ranges", "public_context"}, f"result.requests[{number}]")
            for name in ("kind", "question", "purpose"):
                text(request[name], f"result.requests[{number}].{name}")
            if request["known_source_ranges"] is not None:
                for range_number, source_range in enumerate(arr(request["known_source_ranges"], f"result.requests[{number}].known_source_ranges")):
                    self._range(source_range, f"result.requests[{number}].known_source_ranges[{range_number}]", material_ids)
            nullable_text(request["public_context"], f"result.requests[{number}].public_context")
        verified_urls = {str(source.get("url")) for raw in arr(packet.get("verified_sources", []), "packet.verified_sources") if (source := obj(raw, "verified source")).get("quote_verified") is True and source.get("url")}
        markdown_urls = {
            value.rstrip(".,，。；;")
            for value in re.findall(r"https?://[^\s<>)\]]+", markdown)
        }
        invented = markdown_urls - verified_urls
        if invented:
            raise EvidenceError(f"markdown references URL absent from verified_sources: {sorted(invented)[0]}")
        return deepcopy(dict(result))
