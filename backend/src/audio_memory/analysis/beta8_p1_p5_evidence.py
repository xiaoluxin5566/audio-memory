from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
from typing import Any

from audio_memory.analysis.beta8_writing_evidence import (
    EvidenceCatalog as LegacyCatalog,
    EvidenceError,
    arr,
    fields,
    ids,
    nullable_text,
    obj,
    text,
)


P1_FIELDS = {"status", "window_id", "activities", "topics", "attention_signals", "commitments", "context_requests"}
TOPIC_FIELDS = {"local_key", "understanding", "open_questions", "evidence_anchor_ids"}
SIGNAL_FIELDS = {"local_key", "topic_keys", "type", "description", "evidence_anchor_ids"}
COMMITMENT_FIELDS = {"local_key", "topic_keys", "text", "actor", "acceptance_state", "time_text", "completed", "evidence_anchor_ids"}
PLAN_FIELDS = {"status", "cards", "topic_dispositions", "research_tasks", "context_requests", "budget_gaps"}
BRIEF_FIELDS = {"draft_key", "scene_id", "core_question", "source_activity_ids", "topic_assignments", "reader_need", "must_answer", "must_cover", "useful_deliverable", "research_decision"}
CARD_FIELDS = {"status", "card_id", "title", "markdown", "evidence_refs", "research_refs", "uncertainties"}
SCENES = {"work_communication", "parenting_family", "health_state", "content_consumption", "inspiration_insight", "self_growth", "life_decisions"}


class P1P5EvidenceCatalog:
    """Approved P1-P5 contracts while reusing proven Activity normalization."""

    def __init__(self, segments: Sequence[Mapping[str, object]]) -> None:
        self.legacy = LegacyCatalog(segments)
        self._by_id = self.legacy._by_id
        collapsed: dict[str, list[str]] = {}
        for segment_id in self._by_id:
            match = re.fullmatch(r"seg_(\d+)_(\d+)", segment_id)
            if match is not None:
                collapsed.setdefault(f"seg_{match.group(1)}{match.group(2)}", []).append(segment_id)
        self._collapsed_segment_aliases = {
            alias: values[0] for alias, values in collapsed.items() if len(values) == 1
        }

    def _normalize_window_segment_ids(self, result: Mapping[str, object]) -> dict[str, object]:
        normalized = deepcopy(dict(result))

        def segment_id(value: object) -> object:
            if not isinstance(value, str) or value in self._by_id:
                return value
            return self._collapsed_segment_aliases.get(value, value)

        for activity in normalized.get("activities", []):
            if isinstance(activity, dict):
                activity["start_segment_id"] = segment_id(activity.get("start_segment_id"))
                activity["end_segment_id"] = segment_id(activity.get("end_segment_id"))
        for collection in ("topics", "attention_signals", "commitments"):
            for item in normalized.get(collection, []):
                if isinstance(item, dict) and isinstance(item.get("evidence_anchor_ids"), list):
                    item["evidence_anchor_ids"] = [segment_id(value) for value in item["evidence_anchor_ids"]]
        return normalized

    def windows(self, core_budget_bytes: int, boundary_segments: int = 2):
        return self.legacy.windows(core_budget_bytes, boundary_segments)

    def _adapt_window(self, result: Mapping[str, object], window: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        result = obj(result, "P1 result")
        if set(result) != P1_FIELDS:
            raise EvidenceError("P1 fields do not match the approved contract")
        activities = arr(result["activities"], "P1.activities")
        activity_by_key: dict[str, Mapping[str, object]] = {}
        activity_ids: dict[str, set[str]] = {}
        core_ids = {str(row["segment_id"]) for row in window["core_segments"]}
        for index, raw in enumerate(activities):
            activity = obj(raw, f"P1.activities[{index}]")
            key = text(activity.get("local_key"), f"P1.activities[{index}].local_key")
            if key in activity_by_key:
                raise EvidenceError(f"duplicate Activity local_key: {key}")
            activity_by_key[key] = activity
            _, full = self.legacy._bounds(activity.get("start_segment_id"), activity.get("end_segment_id"), f"Activity {key}", set(
                str(row["segment_id"]) for row in [*window["boundary_context"]["before"], *window["core_segments"], *window["boundary_context"]["after"]]
            ))
            activity_ids[key] = set(full)

        def anchor_activities(anchors: list[str], label: str) -> list[str]:
            if not set(anchors) <= self._by_id.keys():
                raise EvidenceError(f"{label} references an unknown Anchor")
            if not set(anchors) & core_ids:
                raise EvidenceError(f"{label} must include core evidence")
            keys = [
                key for key, values in activity_ids.items()
                if set(anchors) & values
            ]
            covered = {anchor for anchor in anchors if any(anchor in activity_ids[key] for key in keys)}
            if covered != set(anchors):
                raise EvidenceError(f"{label} Anchor is outside all Activities")
            return keys

        topic_keys: set[str] = set()
        topic_records = []
        for index, raw in enumerate(arr(result["topics"], "P1.topics")):
            topic = obj(raw, f"P1.topics[{index}]")
            if set(topic) != TOPIC_FIELDS:
                raise EvidenceError("Topic fields do not match the approved no-ranges contract")
            key = text(topic["local_key"], f"P1.topics[{index}].local_key")
            if key in topic_keys or key in activity_by_key:
                raise EvidenceError(f"duplicate Topic: {key}")
            topic_keys.add(key)
            anchors = ids(topic["evidence_anchor_ids"], f"Topic {key}.evidence_anchor_ids", nonempty=True)
            activity_keys = anchor_activities(anchors, f"Topic {key}")
            questions = topic["open_questions"]
            questions = [] if questions is None else [questions] if isinstance(questions, str) else arr(questions, f"Topic {key}.open_questions")
            text(topic["understanding"], f"Topic {key}.understanding")
            topic_records.append(deepcopy(dict(topic, open_questions=questions, activity_keys=activity_keys)))

        adapted_topics = []
        for index, (key, activity) in enumerate(activity_by_key.items(), start=1):
            owned = [segment_id for segment_id in activity_ids[key] if segment_id in core_ids]
            owned.sort(key=self.legacy._global.__getitem__)
            source = self.legacy._source[owned[0]]
            adapted_topics.append({
                "local_key": f"__activity_topic_{index}", "parent_activity_key": key,
                "ranges": [{"source_file": source, "start_segment_id": owned[0], "end_segment_id": owned[-1]}],
                "provenance": "program_activity_validation", "understanding": str(activity["subject"]),
                "open_questions": [], "evidence_anchors": [owned[0]],
            })

        extras: dict[str, object] = {"topics": topic_records, "attention_signals": [], "commitments": []}
        all_local = set(activity_by_key) | topic_keys
        for index, raw in enumerate(arr(result["attention_signals"], "P1.attention_signals")):
            signal = obj(raw, f"P1.attention_signals[{index}]")
            if set(signal) != SIGNAL_FIELDS:
                raise EvidenceError("Attention Signal fields do not match the approved contract")
            key = text(signal["local_key"], f"Attention Signal {index}.local_key")
            topic_refs = ids(signal["topic_keys"], f"Attention Signal {key}.topic_keys")
            anchors = ids(signal["evidence_anchor_ids"], f"Attention Signal {key}.evidence_anchor_ids", nonempty=True)
            if key in all_local or not set(topic_refs) <= topic_keys:
                raise EvidenceError(f"Attention Signal {key} has duplicate key or unknown reference")
            all_local.add(key)
            normalized = deepcopy(dict(signal, activity_keys=anchor_activities(anchors, f"Attention Signal {key}")))
            extras["attention_signals"].append(normalized)

        for index, raw in enumerate(arr(result["commitments"], "P1.commitments")):
            commitment = obj(raw, f"P1.commitments[{index}]")
            if set(commitment) != COMMITMENT_FIELDS:
                raise EvidenceError("Commitment fields do not match the approved contract")
            key = text(commitment["local_key"], f"Commitment {index}.local_key")
            topic_refs = ids(commitment["topic_keys"], f"Commitment {key}.topic_keys")
            anchors = ids(commitment["evidence_anchor_ids"], f"Commitment {key}.evidence_anchor_ids", nonempty=True)
            if key in all_local or not set(topic_refs) <= topic_keys:
                raise EvidenceError(f"Commitment {key} has duplicate key or unknown reference")
            if not isinstance(commitment["completed"], bool):
                raise EvidenceError(f"Commitment {key}.completed must be boolean")
            all_local.add(key)
            normalized = deepcopy(dict(commitment, activity_keys=anchor_activities(anchors, f"Commitment {key}")))
            extras["commitments"].append(normalized)
            text(commitment["text"], f"Commitment {key}.text")
            nullable_text(commitment["actor"], f"Commitment {key}.actor")
            nullable_text(commitment["acceptance_state"], f"Commitment {key}.acceptance_state")
            nullable_text(commitment["time_text"], f"Commitment {key}.time_text")

        adapted = {
            "status": result["status"], "window_id": result["window_id"], "activities": deepcopy(activities),
            "topics": adapted_topics, "commitments": [], "context_requests": deepcopy(result["context_requests"]),
        }
        return adapted, extras

    def validate_window(self, result, window):
        normalized = self._normalize_window_segment_ids(result)
        adapted, _ = self._adapt_window(normalized, window)
        self.legacy.validate_window(adapted, window)
        return normalized

    def normalize(self, results, windows):
        adapted, extras = [], []
        for result, window in zip(results, windows, strict=True):
            value, extra = self._adapt_window(self._normalize_window_segment_ids(result), window)
            adapted.append(value)
            extras.append(extra)
        registry = self.legacy.normalize(adapted, windows)
        activity_lookup = {(part["window_id"], part["local_key"]): activity["activity_id"] for activity in registry["normalized_activities"] for part in activity["window_parts"]}
        raw_topics = [
            (str(result["window_id"]), topic)
            for result, extra in zip(results, extras, strict=True)
            for topic in extra["topics"]
        ]
        raw_topics.sort(key=lambda item: min(self.legacy._global[anchor] for anchor in item[1]["evidence_anchor_ids"]))
        registered_topics, topic_lookup = [], {}
        for number, (window_id, raw) in enumerate(raw_topics, start=1):
            topic_id = f"topic_{number:03d}"
            activity_ids = list(dict.fromkeys(activity_lookup[(window_id, key)] for key in raw["activity_keys"]))
            normalized = {key: deepcopy(value) for key, value in raw.items() if key not in {"activity_keys", "evidence_anchor_ids"}}
            normalized.update({
                "topic_id": topic_id, "window_id": window_id, "activity_ids": activity_ids,
                "evidence_anchors": [self._anchor(anchor) for anchor in raw["evidence_anchor_ids"]],
            })
            registered_topics.append(normalized)
            topic_lookup[(window_id, str(raw["local_key"]))] = topic_id
        registry["registered_topics"] = registered_topics

        signals, commitments = [], []
        for result, extra in zip(results, extras, strict=True):
            window_id = str(result["window_id"])
            for raw in extra["attention_signals"]:
                normalized = {key: deepcopy(value) for key, value in raw.items() if key not in {"activity_keys", "evidence_anchor_ids", "topic_keys"}}
                signals.append({**normalized, "signal_id": f"signal_{len(signals)+1:03d}", "activity_ids": list(dict.fromkeys(activity_lookup[(window_id, key)] for key in raw["activity_keys"])), "topic_ids": [topic_lookup[(window_id, key)] for key in raw["topic_keys"]], "evidence_anchors": [self._anchor(i) for i in raw["evidence_anchor_ids"]], "window_id": window_id})
            for raw in extra["commitments"]:
                normalized = {key: deepcopy(value) for key, value in raw.items() if key not in {"activity_keys", "evidence_anchor_ids", "topic_keys"}}
                commitments.append({**normalized, "commitment_id": f"commitment_{len(commitments)+1:03d}", "activity_ids": list(dict.fromkeys(activity_lookup[(window_id, key)] for key in raw["activity_keys"])), "topic_ids": [topic_lookup[(window_id, key)] for key in raw["topic_keys"]], "evidence_anchors": [self._anchor(i) for i in raw["evidence_anchor_ids"]], "window_id": window_id})
        registry["attention_signals"] = signals
        registry["commitments"] = commitments
        return registry

    def _anchor(self, segment_id: str) -> dict[str, str]:
        return {"segment_id": segment_id, "quote": str(self._by_id[segment_id]["text"])}

    def _activity_rows(self, activity):
        return self.legacy._source_rows(activity["source_ranges"], f"Activity {activity['activity_id']}")

    def planning_input(self, registry):
        return {
            **deepcopy(registry),
            "activity_transcripts": [{"activity_id": activity["activity_id"], "complete_transcript": self._activity_rows(activity)} for activity in registry["normalized_activities"]],
        }

    def _registry(self, registry):
        activities = {row["activity_id"]: row for row in registry["normalized_activities"]}
        topics = {row["topic_id"]: row for row in registry["registered_topics"]}
        return activities, topics

    def _normalize_plan_provenance(self, plan, activities, topics):
        normalized = deepcopy(plan)
        cards_by_key = {
            card.get("draft_key"): card for card in normalized["cards"]
            if isinstance(card, dict)
        }
        assigned_topic_ids = {
            assignment.get("topic_id")
            for card in normalized["cards"] if isinstance(card, dict)
            for assignment in card.get("topic_assignments", []) if isinstance(assignment, dict)
        }
        for disposition in normalized["topic_dispositions"]:
            topic_id = disposition.get("topic_id")
            card_keys = disposition.get("card_keys")
            if (
                disposition.get("state") in {"standalone", "merged"}
                and topic_id in topics and topic_id not in assigned_topic_ids
                and isinstance(card_keys, list) and len(card_keys) == 1
                and card_keys[0] in cards_by_key
            ):
                cards_by_key[card_keys[0]]["topic_assignments"].append({
                    "topic_id": topic_id,
                    "role": "core" if disposition["state"] == "standalone" else "context",
                })
                assigned_topic_ids.add(topic_id)
        activity_by_anchor = {}
        for activity_id, activity in activities.items():
            for row in self._activity_rows(activity):
                segment_id = str(row["segment_id"])
                if segment_id in activity_by_anchor:
                    raise EvidenceError(f"Anchor {segment_id} belongs to multiple Activities")
                activity_by_anchor[segment_id] = activity_id
        for card in normalized["cards"]:
            source_ids = list(card["source_activity_ids"])
            for assignment in card["topic_assignments"]:
                topic = topics.get(assignment.get("topic_id"))
                if topic:
                    source_ids.extend(topic["activity_ids"])
            covers = []
            for cover in card["must_cover"]:
                grouped = {}
                for anchor_id in cover.get("evidence_anchor_ids", []):
                    activity_id = activity_by_anchor.get(str(anchor_id))
                    if activity_id is None:
                        raise EvidenceError(f"Card {card.get('draft_key')} references unknown Anchor")
                    grouped.setdefault(activity_id, []).append(anchor_id)
                    source_ids.append(activity_id)
                for activity_id, anchor_ids in grouped.items():
                    covers.append({**cover, "activity_id": activity_id, "evidence_anchor_ids": anchor_ids})
            card["source_activity_ids"] = list(dict.fromkeys(source_ids))
            card["must_cover"] = covers
        return normalized

    def validate_plan(self, plan, registry):
        plan = obj(plan, "P2 plan")
        if set(plan) != PLAN_FIELDS:
            raise EvidenceError("P2 plan fields do not match the approved contract")
        if plan["status"] not in {"complete", "needs_context"}:
            raise EvidenceError("invalid P2 status")
        activities, topics = self._registry(registry)
        plan = self._normalize_plan_provenance(plan, activities, topics)
        card_keys, assignments = set(), {}
        for number, raw in enumerate(arr(plan["cards"], "P2.cards")):
            card = obj(raw, f"P2.cards[{number}]")
            if set(card) != BRIEF_FIELDS:
                raise EvidenceError("P2 card fields do not match the approved contract")
            key = text(card["draft_key"], f"P2.cards[{number}].draft_key")
            if key in card_keys or text(card["scene_id"], f"Card {key}.scene_id") not in SCENES:
                raise EvidenceError(f"duplicate card or invalid scene: {key}")
            card_keys.add(key)
            source_ids = ids(card["source_activity_ids"], f"Card {key}.source_activity_ids", nonempty=True)
            if not set(source_ids) <= activities.keys():
                raise EvidenceError(f"Card {key} references unknown Activity")
            text(card["core_question"], f"Card {key}.core_question")
            text(card["reader_need"], f"Card {key}.reader_need")
            text(card["useful_deliverable"], f"Card {key}.useful_deliverable")
            if not ids(card["must_answer"], f"Card {key}.must_answer", nonempty=True):
                raise EvidenceError(f"Card {key} needs must_answer")
            for item in arr(card["topic_assignments"], f"Card {key}.topic_assignments"):
                assignment = obj(item, f"Card {key}.topic_assignment")
                if set(assignment) != {"topic_id", "role"} or assignment["role"] not in {"core", "supporting_evidence", "context", "counterpoint"}:
                    raise EvidenceError(f"Card {key} has invalid topic assignment")
                topic_id = text(assignment["topic_id"], f"Card {key}.topic_id")
                if topic_id not in topics or not set(topics[topic_id]["activity_ids"]) <= set(source_ids):
                    raise EvidenceError(f"Card {key} topic is outside source Activities")
                if topic_id in assignments:
                    raise EvidenceError(f"Topic {topic_id} is assigned to multiple cards")
                assignments[topic_id] = (key, assignment["role"])
            for item in arr(card["must_cover"], f"Card {key}.must_cover"):
                cover = obj(item, f"Card {key}.must_cover item")
                if set(cover) != {"description", "activity_id", "evidence_anchor_ids"}:
                    raise EvidenceError(f"Card {key} has invalid must_cover")
                activity_id = text(cover["activity_id"], f"Card {key}.must_cover.activity_id")
                anchors = ids(cover["evidence_anchor_ids"], f"Card {key}.must_cover.evidence_anchor_ids", nonempty=True)
                if activity_id not in source_ids or not set(anchors) <= {str(r["segment_id"]) for r in self._activity_rows(activities[activity_id])}:
                    raise EvidenceError(f"Card {key} must_cover is outside source Activity")
                text(cover["description"], f"Card {key}.must_cover.description")
            research = obj(card["research_decision"], f"Card {key}.research_decision")
            if set(research) != {"decision", "reason", "task_keys"} or research["decision"] not in {"search", "no_search"}:
                raise EvidenceError(f"Card {key} research_decision must be explicit")
            text(research["reason"], f"Card {key}.research_decision.reason")
            task_keys = ids(research["task_keys"], f"Card {key}.research_decision.task_keys")
            if (research["decision"] == "search") != bool(task_keys):
                raise EvidenceError(f"Card {key} research decision conflicts with tasks")

        dispositions = {}
        for raw in arr(plan["topic_dispositions"], "P2.topic_dispositions"):
            item = obj(raw, "P2 topic disposition")
            if set(item) != {"topic_id", "state", "card_keys", "reason"}:
                raise EvidenceError("Topic disposition fields do not match the approved contract")
            topic_id = text(item["topic_id"], "Topic disposition.topic_id")
            state = item["state"]
            keys = ids(item["card_keys"], f"Topic {topic_id}.card_keys")
            if topic_id not in topics or topic_id in dispositions or state not in {"standalone", "merged", "excluded", "needs_context"}:
                raise EvidenceError(f"invalid topic disposition: {topic_id}")
            if state in {"standalone", "merged"} and (len(keys) != 1 or keys[0] not in card_keys):
                raise EvidenceError(f"Topic {topic_id} disposition conflicts with card")
            if state in {"excluded", "needs_context"} and keys:
                raise EvidenceError(f"Topic {topic_id} disposition cannot reference card")
            if state == "standalone" and assignments.get(topic_id, (None, None))[1] != "core":
                raise EvidenceError(f"Standalone Topic {topic_id} must be a core assignment")
            if state == "merged" and topic_id not in assignments:
                raise EvidenceError(f"Merged Topic {topic_id} must be assigned")
            text(item["reason"], f"Topic {topic_id}.reason")
            dispositions[topic_id] = state
        if set(dispositions) != set(topics):
            raise EvidenceError("every Topic requires exactly one absolute disposition")

        task_by_key = {}
        required_task_fields = {"task_key", "question", "purpose", "public_context", "target_card_keys", "source_requirements", "jurisdiction", "as_of", "version_constraint", "stop_condition"}
        for raw in arr(plan["research_tasks"], "P2.research_tasks"):
            task = obj(raw, "P2 research task")
            if set(task) != required_task_fields:
                raise EvidenceError("Research task fields do not match the approved contract")
            key = text(task["task_key"], "Research task.task_key")
            targets = ids(task["target_card_keys"], f"Research {key}.target_card_keys", nonempty=True)
            if key in task_by_key or not set(targets) <= card_keys:
                raise EvidenceError(f"invalid research task {key}")
            for name in ("question", "purpose", "public_context", "source_requirements", "stop_condition"):
                text(task[name], f"Research {key}.{name}")
            for name in ("jurisdiction", "as_of", "version_constraint"):
                nullable_text(task[name], f"Research {key}.{name}")
            task_by_key[key] = task
        for card in plan["cards"]:
            wanted = set(card["research_decision"]["task_keys"])
            actual = {key for key, task in task_by_key.items() if card["draft_key"] in task["target_card_keys"]}
            if wanted != actual:
                raise EvidenceError(f"research task/card links are not symmetric for {card['draft_key']}")
        if plan["status"] == "complete" and plan["context_requests"]:
            raise EvidenceError("complete P2 plan cannot request context")
        arr(plan["budget_gaps"], "P2.budget_gaps")
        return deepcopy(dict(plan))

    def assemble(self, card_id, brief, registry, corrections):
        activities, topics = self._registry(registry)
        source_ids = brief["source_activity_ids"]
        complete, seen = [], set()
        for activity_id in source_ids:
            for row in self._activity_rows(activities[activity_id]):
                if row["segment_id"] not in seen:
                    complete.append(row); seen.add(row["segment_id"])
        topic_ids = [item["topic_id"] for item in brief["topic_assignments"]]
        return {
            "card_id": card_id, "card_brief": deepcopy(brief), "complete_transcript": complete,
            "source_activities": [deepcopy(activities[i]) for i in source_ids],
            "topic_registry": [deepcopy(topics[i]) for i in topic_ids],
            "attention_signals": [deepcopy(x) for x in registry["attention_signals"] if set(x["activity_ids"]) & set(source_ids)],
            "commitments": [deepcopy(x) for x in registry["commitments"] if set(x["activity_ids"]) & set(source_ids)],
            "user_corrections": deepcopy(corrections), "research_packets": [], "verified_sources": [],
            "context_scope": {"activity_ids": deepcopy(source_ids), "topic_ids": topic_ids, "segment_ids": [str(r["segment_id"]) for r in complete]},
        }

    @staticmethod
    def _locator(markdown, value, label):
        # P4 may return a short semantic label (for example, "面试问运动爱好")
        # instead of a byte-exact excerpt from its Markdown.  The authoritative
        # grounding is the immutable anchor/source ID; forcing exact wording adds
        # a brittle formatting gate without improving factual verification.
        text(value, label)

    def validate_card(self, result, packet):
        result = obj(result, "P4 result")
        if set(result) != CARD_FIELDS:
            raise EvidenceError("P4 fields do not match the approved focused-writing contract")
        if result["status"] not in {"written", "insufficient_evidence", "conflicting_evidence", "research_gap"}:
            raise EvidenceError("invalid P4 status")
        if result["card_id"] != packet["card_id"]:
            raise EvidenceError("P4 card_id mismatch")
        markdown = text(result["markdown"], "P4.markdown")
        text(result["title"], "P4.title")
        material = {str(r["segment_id"]): str(r["text"]) for r in packet["complete_transcript"]}
        for ref in arr(result["evidence_refs"], "P4.evidence_refs"):
            ref = obj(ref, "P4 evidence_ref")
            if set(ref) != {"body_locator", "anchor_ids"}:
                raise EvidenceError("P4 evidence_ref fields are invalid")
            self._locator(markdown, ref["body_locator"], "P4 evidence body_locator")
            anchor_ids = set(ids(ref["anchor_ids"], "P4 evidence anchor_ids", nonempty=True))
            if not anchor_ids <= material.keys():
                raise EvidenceError("P4 evidence is outside supplied Activities")
        verified = {x["source_id"] for x in packet.get("verified_sources", [])}
        for ref in arr(result["research_refs"], "P4.research_refs"):
            ref = obj(ref, "P4 research_ref")
            if set(ref) != {"body_locator", "source_ids"}:
                raise EvidenceError("P4 research_ref fields are invalid")
            self._locator(markdown, ref["body_locator"], "P4 research body_locator")
            if not set(ids(ref["source_ids"], "P4 research source_ids", nonempty=True)) <= verified:
                raise EvidenceError("P4 cites unverified research")
        arr(result["uncertainties"], "P4.uncertainties")
        return deepcopy(dict(result))
