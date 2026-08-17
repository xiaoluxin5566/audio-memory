#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from audio_memory.analysis.markdown_report import append_report_metrics


RUN_PATHS = (
    ("2026-07-29", "outputs/deepseek-audited-report/value-quality-v3-2026-07-29"),
    ("2026-07-30", "outputs/deepseek-audited-report/value-quality-v3-2026-07-30"),
)


def report_card(date: str, run_dir: Path) -> dict[str, object]:
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")
    comparison = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
    current = comparison["new"]
    v1_audit = json.loads((run_dir / "v1-audit.json").read_text(encoding="utf-8"))
    markdown = append_report_metrics(
        markdown,
        initial_score=v1_audit["scores"]["total"],
        final_score=current["final_score"],
        revised=current["published_version"] == "v2",
    )
    final_audit_path = run_dir / "v2-final-audit.json"
    final_audit = (
        json.loads(final_audit_path.read_text(encoding="utf-8"))
        if final_audit_path.exists()
        else None
    )
    quality_passed = bool(final_audit and final_audit.get("passed"))
    usage = comparison.get("provider_usage_all_evaluation_calls", {})
    title_match = re.search(r"^#\s+(.+)$", markdown, re.M)
    paragraphs = [
        item.strip() for item in re.split(r"\n\s*\n", markdown)
        if item.strip() and not item.lstrip().startswith("#")
    ]
    score_scope = (
        "v2_final_audit"
        if current["published_version"] == "v2"
        else "v1_full_audit"
    )
    return {
        "id": f"deepseek-audited-report-{date}",
        "batch_id": f"deepseek-audited-preview-{date}",
        "scene_id": "analysis",
        "uploaded_at": f"{date}T23:59:00+08:00",
        "qa": [], "evidence": [],
        "payload": {
            "scene_id": "analysis",
            "cards": [{
                "title": title_match.group(1).strip() if title_match else "全天录音分析",
                "summary": re.sub(r"\s+", " ", paragraphs[0])[:240] if paragraphs else "",
                "external_source_ids": [], "evidence_segment_ids": [],
            }],
            "reportMarkdown": markdown,
            "reportQuality": {
                "report_version": current["published_version"],
                "audit_status": (
                    "completed" if quality_passed
                    else "completed_v2_final_audit_degraded"
                ),
                "quality_score": current["final_score"],
                "quality_score_scope": score_scope,
                "quality_passed": quality_passed,
            },
            "runtimeMetrics": {
                "model_call_count": current["production_call_count"],
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "web_search_performed": False,
                "model_duration_ms": round(current["measured_elapsed_seconds"] * 1000),
                "final_quality_passed": quality_passed,
                "revised": current["published_version"] == "v2",
                "stop_reason": None,
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        type=Path,
        default=ROOT,
        help="包含既有 outputs/ 的只读项目根目录",
    )
    args = parser.parse_args()
    payload = {
        "days": [
            {"date": date, "cards": [report_card(date, args.source_root / run_path)]}
            for date, run_path in reversed(RUN_PATHS)
        ],
        "todos": [],
    }
    target = ROOT / "prototype/output/deepseek-historical-report-preview.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
