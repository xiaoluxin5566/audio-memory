from __future__ import annotations

import json
from hashlib import sha256

import pytest


def score(card: dict) -> dict:
    return {
        "card_id": card["card_id"], "title": card["title"],
        "card_sha256": sha256(card["markdown"].encode()).hexdigest(),
        "rubric_version": "beta8-quality-2026-09-09-scaled", "status": "scored",
        "dimensions": {"factual_accuracy": 15, "important_coverage": 15,
                       "analysis_depth": 25, "actionability": 30,
                       "expression_structure": 15},
        "raw_total": 100, "capped_reference_total": None, "deductions": [],
        "read_scope": ["activity_1"], "available_scope": "complete_activities",
        "strengths": "证据完整", "weaknesses": "无", "missing_inputs": [],
    }


@pytest.mark.parametrize("card_status", ["written", "research_gap"])
def test_build_publication_maps_cards_scores_evidence_and_sources(
    tmp_path, card_status: str
) -> None:
    from audio_memory.analysis.beta8_p1_p5_publication import build_publication
    from audio_memory.prompts.beta8_pipeline_schema import stable_source_id

    markdown = "# 行程安排\n\n## 1. 明日安排\n\n已经约定明天出发。"
    card = {"status": card_status, "card_id": "card-1", "title": "行程安排",
            "markdown": markdown,
            "evidence_refs": [{"anchor_ids": ["seg_0_0"]}],
            "research_refs": [{"source_ids": [stable_source_id("https://example.com/guide")]}],
            "uncertainties": []}
    (tmp_path / "card-plan.json").write_text(json.dumps({
        "status": "complete", "cards": [{"draft_key": "trip", "scene_id": "parenting_family"}],
        "research_tasks": [{"task_key": "search-1"}], "budget_gaps": []
    }, ensure_ascii=False))
    (tmp_path / "research-packets.json").write_text(json.dumps({"search-1": {
        "task_key": "search-1", "status": "sufficient", "answer": "景点信息",
        "findings": [], "unresolved_questions": [], "sources": [{
            "source_id": stable_source_id("https://example.com/guide"),
            "title": "官方指南", "url": "https://example.com/guide", "publisher": "Example",
            "published_at": "2026-09-01", "quote_verified": True,
            "fetch": {"fetched_at": "2026-09-10T00:00:00+00:00"}
        }]
    }}, ensure_ascii=False))
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards/card-1.input.json").write_text(json.dumps({
        "card_brief": {"scene_id": "parenting_family"}
    }, ensure_ascii=False))

    publication = build_publication(
        {"status": "scored_v1", "cards": [card], "scores": [score(card)]},
        artifact_dir=tmp_path,
    )

    assert publication.bundle.cards[0].scene_id == "parenting_family"
    assert publication.bundle.cards[0].source_segment_ids == ["seg_0_0"]
    assert publication.bundle.cards[0].used_source_ids == [stable_source_id("https://example.com/guide")]
    assert publication.bundle.external_sources[0].title == "官方指南"
    assert publication.card_assessments["card-1"]["raw_total"] == 100


def test_build_publication_rejects_incomplete_result(tmp_path) -> None:
    from audio_memory.analysis.beta8_p1_p5_publication import build_publication

    with pytest.raises(ValueError, match="scored_v1"):
        build_publication({"status": "draft_ready", "cards": [], "scores": []}, artifact_dir=tmp_path)
