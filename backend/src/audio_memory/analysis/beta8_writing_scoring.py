from __future__ import annotations

from hashlib import sha256
from copy import deepcopy
import math


RUBRIC_VERSION = "beta8-quality-2026-09-09-scaled"
DIMENSIONS = {"factual_accuracy": 15, "important_coverage": 15, "analysis_depth": 25, "actionability": 30, "expression_structure": 15}
RULES = {
    "factual_accuracy": {"F1": 4, "F2": (6, 8), "F3": 3, "F4": 2.5, "F5": 1.5, "F6": .5, "F7": 2, "F_CRITICAL": (6, 8)},
    "important_coverage": {"COV1": 3.6, "COV2": 2.4, "COV3": 1.2, "COV4": 1.8, "COV5": 3, "COV_WHOLE": (3, 3.6)},
    "analysis_depth": {"D1": 5, "D2": 3.75, "D3": 3.75, "D4": 2.5, "D5": 5, "D_NONE": (6.25, 8.75)},
    "actionability": {"A1": 8, "A2": 4, "A3": 6, "A4": 6, "A5": 2, "A_NONE": (8, 12), "A_SEARCH_MISSING": 4},
    "expression_structure": {"E1": 6, "E2": 3, "E3": 3, "E4": 3, "E5": (1.5, 3), "E6": 4.5},
}


def pending_score(card: dict, packet: dict) -> dict:
    return {
        "card_id": card["card_id"], "title": card.get("title", ""),
        "card_sha256": sha256(card["markdown"].encode()).hexdigest(),
        "rubric_version": RUBRIC_VERSION, "status": "pending",
        "dimensions": dict.fromkeys(DIMENSIONS), "raw_total": None, "capped_reference_total": None,
        "deductions": [], "read_scope": [], "available_scope": packet.get("context_scope"),
        "strengths": None, "weaknesses": None, "missing_inputs": [],
    }


def validate_score(record: dict, card: dict) -> dict:
    expected = pending_score(card, {})
    if record.get("card_id") != card["card_id"] or record.get("card_sha256") != expected["card_sha256"]:
        raise ValueError("Score card hash or identity mismatch")
    if record.get("rubric_version") != RUBRIC_VERSION:
        raise ValueError("Unknown rubric version")
    values = record.get("dimensions", {})
    if set(values) != set(DIMENSIONS):
        raise ValueError("All five dimensions are required")
    complete = record.get("status") == "scored"
    if record.get("status") not in {"scored", "pending"}:
        raise ValueError("Unknown assessment status")
    if complete and record.get("missing_inputs"):
        raise ValueError("Incomplete assessment must stay pending")
    if complete and not record.get("read_scope"):
        raise ValueError("Actual evidence read_scope is required")
    def numeric(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    for name, maximum in DIMENSIONS.items():
        if not complete and values[name] is None:
            continue
        if not numeric(values[name]) or not 0 <= values[name] <= maximum:
            raise ValueError("Score dimension outside its range")
    deductions = record.get("deductions")
    if not isinstance(deductions, list):
        raise ValueError("deductions must be a list")
    seen = set()
    totals = dict.fromkeys(DIMENSIONS, 0.0)
    for item in deductions:
        dimension = item.get("dimension")
        if dimension not in DIMENSIONS or not numeric(item.get("points")) or item["points"] <= 0:
            raise ValueError("Invalid deduction")
        for field in ("rule", "body_locator", "reason", "evidence", "missing_content"):
            if not item.get(field):
                raise ValueError("Every deduction requires rule, location, evidence and missing_content")
        rule = item["rule"]
        allowed = RULES[dimension].get(rule)
        if allowed is None:
            raise ValueError("Unknown or misassigned deduction rule")
        if isinstance(allowed, tuple):
            valid = item["points"] in allowed if rule == "F2" else allowed[0] <= item["points"] <= allowed[1]
            if not item.get("impact_reason"):
                raise ValueError("Range rule requires an impact_reason for the chosen amount")
        else:
            valid = abs(item["points"] - allowed) < .000001
        if not valid or round(item["points"], 2) != item["points"]:
            raise ValueError("Deduction points do not match the approved rule")
        if item.get("severity") not in {"critical", "major", "minor"}:
            raise ValueError("Deduction severity is required")
        if rule == "F_CRITICAL" and item["severity"] != "critical":
            raise ValueError("Critical factual rule requires critical severity")
        if dimension == "factual_accuracy" and item["severity"] == "critical" and item["points"] < 6:
            raise ValueError("critical factual deductions below 6 must use F_CRITICAL")
        key = (dimension, item.get("defect_id") or item["body_locator"])
        if key in seen:
            raise ValueError("Duplicate deduction")
        seen.add(key)
        totals[dimension] += item["points"]
    for name, maximum in DIMENSIONS.items():
        if not complete and values[name] is None:
            continue
        if abs(values[name] - max(0, maximum - totals[name])) > 0.001:
            raise ValueError("Score and deduction totals do not agree")
    if not complete:
        return dict(record, raw_total=None, capped_reference_total=None,
                    acceptance_total=None, passed=None)
    total = round(sum(values.values()), 2)
    result = dict(record, raw_total=total, capped_reference_total=None)
    cap = 59 if any(item["severity"] == "critical" for item in deductions) else 69 if any(item["severity"] == "major" and item["dimension"] == "factual_accuracy" for item in deductions) else None
    if record.get("severity_cap") is not None and record["severity_cap"] != cap:
        raise ValueError("Severity cap disagrees with recorded evidence")
    if cap is not None:
        result["capped_reference_total"] = min(total, cap)
    result["acceptance_total"] = min(total, cap) if cap is not None else total
    result["passed"] = (
        total >= 75
        and not any(item["severity"] in {"critical", "major"} for item in deductions)
    )
    return result


def apply_objective_deductions(record: dict, card: dict, packet: dict) -> dict:
    """Apply only defects that can be proven from pipeline state without semantics."""
    validated = validate_score(record, card)
    if validated.get("status") != "scored":
        return validated
    brief = packet.get("card_brief") if isinstance(packet, dict) else None
    research = brief.get("research_decision") if isinstance(brief, dict) else None
    requires_search = (
        isinstance(research, dict) and research.get("decision") == "search"
    )
    verified_sources = packet.get("verified_sources") if isinstance(packet, dict) else None
    if not requires_search or isinstance(verified_sources, list) and verified_sources:
        return validated
    if any(item.get("rule") == "A_SEARCH_MISSING" for item in validated["deductions"]):
        return validated

    result = deepcopy(validated)
    result["deductions"].append({
        "defect_id": "objective:required_search_unavailable",
        "dimension": "actionability",
        "points": 4,
        "rule": "A_SEARCH_MISSING",
        "severity": "major",
        "body_locator": "需要外部信息才能完成的行动帮助",
        "reason": "P2 明确判定本卡需要搜索，但 P3 没有产出任何已验证来源。",
        "evidence": str(research.get("reason") or "research_decision=search"),
        "missing_content": (
            "需要先取得并验证能回答搜索任务的外部来源，再将结果转化为"
            "与当前计划直接相关的选项、限制、时间或执行建议。"
        ),
    })
    result["dimensions"]["actionability"] = max(
        0, result["dimensions"]["actionability"] - 4
    )
    return validate_score(result, card)
