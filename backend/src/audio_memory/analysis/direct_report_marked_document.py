from __future__ import annotations

from audio_memory.prompts.direct_report_marked_schema import (
    BulletListBlock,
    DirectReportMarkedDocument,
    NumberedListBlock,
    ParagraphBlock,
    QuoteBlock,
    SubheadingBlock,
    TableBlock,
)


def marked_report_markdown(document: DirectReportMarkedDocument) -> str:
    lines = [f"# {document.title}", "", "## 今天发生了什么，重点改进什么", "", document.overview.summary, ""]
    lines.extend([
        "| 阶段 | 发生的事 | 对应的改进 |",
        "| --- | --- | --- |",
    ])
    for row in document.overview.rows:
        lines.append(f"| {_cell(row.phase)} | {_cell(row.event)} | {_cell(row.improvement)} |")
    for section in document.sections:
        lines.extend(["", f"## {section.title}", ""])
        for block in section.blocks:
            if isinstance(block, ParagraphBlock):
                lines.extend([block.text, ""])
            elif isinstance(block, SubheadingBlock):
                lines.extend([f"### {block.title}", ""])
            elif isinstance(block, QuoteBlock):
                lines.extend([f"> “{block.text.strip('“”')}”", ""])
            elif isinstance(block, BulletListBlock):
                lines.extend([*(f"- {item}" for item in block.items), ""])
            elif isinstance(block, NumberedListBlock):
                lines.extend([*(f"{index}. {item}" for index, item in enumerate(block.items, 1)), ""])
            elif isinstance(block, TableBlock):
                lines.append("| " + " | ".join(_cell(item) for item in block.columns) + " |")
                lines.append("| " + " | ".join("---" for _ in block.columns) + " |")
                lines.extend("| " + " | ".join(_cell(item) for item in row) + " |" for row in block.rows)
                lines.append("")
    return "\n".join(lines).strip() + "\n"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
