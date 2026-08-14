from __future__ import annotations

import re
from dataclasses import dataclass


_UNSAFE = (
    re.compile(r"<\s*(?:script|iframe|object|embed|svg|style|img)\b", re.I),
    re.compile(r"\bon\w+\s*=", re.I),
    re.compile(r"\]\(\s*(?:javascript|data|vbscript|file):", re.I),
)


@dataclass(frozen=True, slots=True)
class MarkdownReportResult:
    title: str
    summary: str
    report_markdown: str
    report_annotations: tuple[dict[str, str], ...] | None = None

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
