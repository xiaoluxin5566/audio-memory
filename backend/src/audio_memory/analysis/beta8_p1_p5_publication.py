from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from audio_memory.analysis.beta8_writing_scoring import validate_score
from audio_memory.prompts.beta8_pipeline_schema import Beta8PublicationBundle


@dataclass(frozen=True, slots=True)
class P1P5Publication:
    bundle: Beta8PublicationBundle
    card_assessments: dict[str, dict]


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path.name}")
    return value


def _summary(markdown: str, title: str) -> str:
    paragraphs = re.split(r"\n\s*\n", markdown.strip())
    for paragraph in paragraphs:
        text = re.sub(r"^#{1,6}\s+", "", paragraph.strip())
        text = re.sub(r"[*_`>]+", "", text).strip()
        if text and text != title:
            return text[:2_000]
    return title


def _used_source_ids(card: dict) -> list[str]:
    result: list[str] = []
    for ref in card.get("research_refs", []):
        for source_id in ref.get("source_ids", []):
            if source_id not in result:
                result.append(source_id)
    return result


def _evidence_ids(card: dict) -> list[str]:
    result: list[str] = []
    for ref in card.get("evidence_refs", []):
        for segment_id in ref.get("anchor_ids", []):
            if segment_id not in result:
                result.append(segment_id)
    return result


def build_publication(result: dict, *, artifact_dir: Path) -> P1P5Publication:
    if result.get("status") != "scored_v1":
        raise ValueError("P1-P5 publication requires status scored_v1")
    cards = result.get("cards")
    scores = result.get("scores")
    if not isinstance(cards, list) or not cards or not isinstance(scores, list):
        raise ValueError("P1-P5 publication requires cards and scores")
    publishable_statuses = {
        "written",
        "insufficient_evidence",
        "conflicting_evidence",
        "research_gap",
    }
    if any(card.get("status") not in publishable_statuses for card in cards):
        raise ValueError("P1-P5 publication requires every card to be a completed draft")

    root = Path(artifact_dir)
    research = _read_json(root / "research-packets.json")
    source_registry: dict[str, dict] = {}
    degraded_tasks: list[str] = []
    for task_key, packet in research.items():
        if not isinstance(packet, dict):
            raise ValueError(f"Research packet {task_key} is invalid")
        if packet.get("status") != "sufficient":
            degraded_tasks.append(str(task_key))
        for source in packet.get("sources", []):
            source_id = source.get("source_id")
            if not source_id or not source.get("quote_verified"):
                continue
            existing = source_registry.get(source_id)
            if existing is not None and existing != source:
                raise ValueError(f"Conflicting source metadata: {source_id}")
            source_registry[source_id] = source

    score_by_id = {score.get("card_id"): score for score in scores if isinstance(score, dict)}
    if len(score_by_id) != len(cards):
        raise ValueError("P1-P5 publication requires one score per card")

    final_cards = []
    assessments = {}
    used_ids: list[str] = []
    hashes = {}
    for position, card in enumerate(cards):
        card_id = card["card_id"]
        packet = _read_json(root / "cards" / f"{card_id}.input.json")
        scene_id = packet.get("card_brief", {}).get("scene_id")
        if not scene_id:
            raise ValueError(f"Card {card_id} has no scene_id")
        score = validate_score(score_by_id.get(card_id, {}), card)
        if score.get("status") != "scored":
            raise ValueError(f"Card {card_id} score is incomplete")
        assessments[card_id] = score
        card_sources = _used_source_ids(card)
        missing_sources = sorted(set(card_sources) - set(source_registry))
        if missing_sources:
            raise ValueError(
                f"Card {card_id} references unverified sources: "
                + ", ".join(missing_sources)
            )
        for source_id in card_sources:
            if source_id not in used_ids:
                used_ids.append(source_id)
        markdown_hash = sha256(card["markdown"].encode()).hexdigest()
        hashes[card_id] = markdown_hash
        final_cards.append({
            "card_id": card_id,
            "scene_id": scene_id,
            "position": position,
            "title": card["title"],
            "summary": _summary(card["markdown"], card["title"]),
            "markdown": card["markdown"],
            "source_segment_ids": _evidence_ids(card),
            "used_source_ids": card_sources,
            "origin_card_ids": [card_id],
            "v1_markdown_sha256": markdown_hash,
            "final_markdown_sha256": markdown_hash,
            "untouched": True,
            "work_communication_unit_ids": [],
        })

    external_sources = []
    for source_id in used_ids:
        source = source_registry[source_id]
        fetched_at = (source.get("fetch") or {}).get("fetched_at") or source.get("fetched_at")
        if not fetched_at:
            raise ValueError(f"Verified source {source_id} has no retrieval time")
        external_sources.append({
            "source_id": source_id,
            "title": source["title"],
            "url": source["url"],
            "publisher": source.get("publisher"),
            "published_at": source.get("published_at"),
            "retrieved_at": fetched_at,
            "source_type": "verified_web_page",
            "supports": [],
        })

    bundle = Beta8PublicationBundle.model_validate({
        "cards": final_cards,
        "todo_candidates": [],
        "external_sources": external_sources,
        "v1_card_hashes": hashes,
        "final_card_hashes": hashes,
        "untouched_card_ids": list(hashes),
        "completed_revision_task_ids": [],
        "search_degraded": bool(degraded_tasks),
        "search_degraded_reason": (
            "Some planned research tasks did not return sufficient verified evidence: "
            + ", ".join(degraded_tasks)
            if degraded_tasks else None
        ),
        "expected_work_communication_unit_ids": [],
    })
    return P1P5Publication(bundle=bundle, card_assessments=assessments)
