from __future__ import annotations

import re
from dataclasses import dataclass


_UNSAFE = (
    re.compile(r"<\s*(?:script|iframe|object|embed|svg|style|img)\b", re.I),
    re.compile(r"\bon\w+\s*=", re.I),
    re.compile(r"\]\(\s*(?:javascript|data|vbscript|file):", re.I),
)
_REPORT_METRICS_MARKER = "<!-- audio-memory-report-metrics -->"


def report_character_count(markdown: str) -> int:
    """Count visible non-whitespace characters, excluding a metrics footer."""
    body = markdown.split(_REPORT_METRICS_MARKER, 1)[0]
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"[`#>*_|\[\]()]", "", body)
    return len(re.sub(r"\s+", "", body))


def append_report_metrics(
    markdown: str,
    *,
    initial_score: int | None,
    final_score: int | None,
    revised: bool,
) -> str:
    body = markdown.split(_REPORT_METRICS_MARKER, 1)[0].rstrip()
    body = re.sub(r"\n\s*---\s*$", "", body).rstrip()
    count = report_character_count(body)
    if revised and initial_score is not None and final_score is not None:
        delta = final_score - initial_score
        signed_delta = f"+{delta}" if delta >= 0 else str(delta)
        score_text = (
            f"定向修改增益：{initial_score} → {final_score}"
            f"（{signed_delta}）；"
        )
    elif final_score is not None:
        score_text = f"首次全量审核：{final_score} 分"
    else:
        score_text = "质量评分：未完成"
    return (
        f"{body}\n\n---\n\n{_REPORT_METRICS_MARKER}\n"
        f"> 本次报告：{count} 字｜{score_text}"
    )


@dataclass(frozen=True, slots=True)
class MarkdownReportResult:
    title: str
    summary: str
    report_markdown: str
    report_annotations: tuple[dict[str, str], ...] | None = None
    quality_metadata: object | None = None

    @classmethod
    def from_markdown(cls, markdown: str) -> "MarkdownReportResult":
        cleaned = markdown.strip()
        if cleaned.startswith("```markdown") and cleaned.endswith("```"):
            cleaned = cleaned[len("```markdown") : -3].strip()
        if not cleaned:
            raise ValueError("report is empty")
        if any(pattern.search(cleaned) for pattern in _UNSAFE):
            raise ValueError("report contains unsafe active content")
        title_match = re.search(r"^#\s+(.+?)\s*$", cleaned, re.M)
        title = title_match.group(1).strip() if title_match else "当天录音综合分析"
        paragraphs = [
            item.strip()
            for item in re.split(r"\n\s*\n", cleaned)
            if item.strip() and not item.lstrip().startswith("#")
        ]
        summary = re.sub(r"\s+", " ", paragraphs[0]) if paragraphs else title
        if len(summary) > 240:
            summary = summary[:237].rstrip() + "…"
        return cls(title=title[:240], summary=summary, report_markdown=cleaned)
