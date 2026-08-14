from __future__ import annotations

from dataclasses import dataclass

from audio_memory.prompts.direct_report_schema import (
    BulletListBlock,
    DirectReportDocument,
    NumberedListBlock,
    ParagraphBlock,
    ReportSubsection,
    SourceQuoteBlock,
    SuggestedWordingBlock,
    TableBlock,
)


@dataclass(frozen=True)
class StructuredReportResult:
    document: DirectReportDocument
    title: str
    summary: str
    report_markdown: str

    @classmethod
    def from_document(cls, document: DirectReportDocument) -> StructuredReportResult:
        return cls(
            document=document,
            title=document.title,
            summary=document.overview.summary,
            report_markdown=document_to_markdown(document),
        )


def document_to_markdown(document: DirectReportDocument) -> str:
    parts = [
        f"# {document.title}",
        "## 今天发生了什么，重点改进什么",
        document.overview.summary,
        _table(
            ["时段", "发生的事", "可以改进"],
            [[row.phase, row.event, row.improvement] for row in document.overview.rows],
        ),
    ]
    for section in document.sections:
        parts.append(f"## {section.title}")
        parts.extend(_serialize_block(block) for block in section.blocks)
    return "\n\n".join(parts) + "\n"


def _serialize_block(block) -> str:
    if isinstance(block, ParagraphBlock):
        return block.text
    if isinstance(block, (SourceQuoteBlock, SuggestedWordingBlock)):
        return f"> {block.text}"
    if isinstance(block, BulletListBlock):
        return "\n".join(f"- {item}" for item in block.items)
    if isinstance(block, NumberedListBlock):
        return "\n".join(f"{index}. {item}" for index, item in enumerate(block.items, 1))
    if isinstance(block, TableBlock):
        return _table(block.columns, block.rows)
    if isinstance(block, ReportSubsection):
        contents = [f"### {block.title}"]
        contents.extend(_serialize_block(child) for child in block.blocks)
        return "\n\n".join(contents)
    raise TypeError(f"unsupported report block: {type(block).__name__}")


def _table(columns: list[str], rows: list[list[str]]) -> str:
    rendered = [
        _table_row(columns),
        _table_row(["---"] * len(columns), escape=False),
    ]
    rendered.extend(_table_row(row) for row in rows)
    return "\n".join(rendered)


def _table_row(cells: list[str], *, escape: bool = True) -> str:
    values = [_table_cell(cell) if escape else cell for cell in cells]
    return f"| {' | '.join(values)} |"


def _table_cell(value: str) -> str:
    return " ".join(value.replace("|", r"\|").splitlines()).strip()
