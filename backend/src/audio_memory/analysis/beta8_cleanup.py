from __future__ import annotations

import re


_SUMMARY_LABEL = re.compile(
    r"^(?P<indent>\s*)(?:\*\*)?核心摘要(?:\*\*)?[::：]\s*(?P<body>.*)$"
)
_BODY_LABEL = re.compile(r"^\s*(?:\*\*)?正文(?:\*\*)?[::：]\s*$")
_MANUAL_HEADING_ORDINAL = re.compile(
    r"^\s*(?:[一二三四五六七八九十]+、|\d+\.\s+|\d+、\s*|[（(][一二三四五六七八九十]+[）)]\s*|[（(]\d+[）)]\s*)"
)


def normalize_markdown_heading_title(title: str) -> str:
    """Remove legacy ordinal prefixes from an already-parsed Markdown heading."""
    return _MANUAL_HEADING_ORDINAL.sub("", title).strip()


def remove_redundant_section_labels(markdown: str) -> str:
    cleaned: list[str] = []
    for line in markdown.splitlines():
        summary = _SUMMARY_LABEL.fullmatch(line)
        if summary is not None:
            body = summary.group("body")
            if body:
                cleaned.append(summary.group("indent") + body)
            continue
        if _BODY_LABEL.fullmatch(line):
            continue
        cleaned.append(line)
    suffix = "\n" if markdown.endswith("\n") else ""
    return "\n".join(cleaned) + suffix
