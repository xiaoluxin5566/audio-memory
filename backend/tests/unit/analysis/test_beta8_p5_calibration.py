from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from audio_memory.analysis.beta8_writing_scoring import (
    DIMENSIONS,
    RUBRIC_VERSION,
    apply_objective_deductions,
    validate_score,
)


FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "beta8" / "p5-calibration.json"
)


@pytest.mark.parametrize(
    "case",
    json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"],
    ids=lambda case: case["id"],
)
def test_fixed_p5_calibration_cases(case: dict) -> None:
    card = {"card_id": "calibration-card", "status": "written", "markdown": "完整测试卡片"}
    deductions = deepcopy(case["deductions"])
    dimension_scores = dict(DIMENSIONS)
    for item in deductions:
        dimension_scores[item["dimension"]] -= item["points"]
    score = {
        "card_id": card["card_id"],
        "card_sha256": sha256(card["markdown"].encode()).hexdigest(),
        "rubric_version": RUBRIC_VERSION,
        "status": "scored",
        "dimensions": dimension_scores,
        "deductions": deductions,
        "read_scope": ["activity_calibration"],
        "strengths": "校准样本",
        "weaknesses": "参见扣分项",
        "missing_inputs": [],
    }
    if case.get("required_search"):
        packet = {
            "card_brief": {
                "research_decision": {
                    "decision": "search",
                    "reason": "该卡需要外部时效信息",
                    "task_keys": ["calibration-search"],
                }
            },
            "verified_sources": [{}] * case["verified_source_count"],
            "research_packets": [],
        }
        result = apply_objective_deductions(score, card, packet)
    else:
        result = validate_score(score, card)

    assert result["raw_total"] == case["expected_raw_total"]
    assert result["passed"] is case["expected_passed"]
    if case.get("expected_program_rule"):
        assert result["deductions"][-1]["rule"] == case["expected_program_rule"]
