from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict


_ATX_HEADING = re.compile(r"^(?P<marks>#{1,6})\s+(?P<body>\S.*?)(?:\s+#+)?\s*$")
_MANUAL_ORDINAL = re.compile(
    r"^(?:[一二三四五六七八九十百千万]+、(?=\S)|\d+、(?=\S)|"
    r"\d+\.(?!\d)|\d+\)(?=\S)|"
    r"[（(][一二三四五六七八九十百千万\d]+[）)](?=\S))"
)
_ORDERED_STEP = re.compile(r"^\s*\d+[.)、]\s+\S")
_UNORDERED_ITEM = re.compile(r"^\s*[-+*]\s+\S")
_BLOCKQUOTE = re.compile(r"^\s*>")
_THEMATIC_BREAK = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$")
_FENCE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")
_TABLE_DELIMITER_CELL = re.compile(r":?-{3,}:?")
_TABLE_DELIMITER_LIKE_CELL = re.compile(r":?-+:?")
_REPORTING_TONE = (
    "这份记录围绕",
    "从这段记录可以看出",
    "分析指向",
    "本次材料显示",
    "逐字稿中提到",
)
_LONG_PARAGRAPH_CHARS = 420


class CardStructureError(ValueError):
    """A deterministic Markdown structure error that preserves line locations."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class CardQualitySignals(BaseModel):
    """Read-only structural signals for the semantic review stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    h2_count: int
    h3_count: int
    table_count: int
    ordered_step_count: int
    blockquote_count: int
    long_paragraph_count: int
    reporting_tone_spans: list[str]
    manual_ordinal_headings: list[str]
    semantic_correctness: None = None


def _without_fenced_code(markdown: str) -> list[str]:
    """Return lines while preserving line numbers and ignoring fenced code content."""
    visible: list[str] = []
    fence: tuple[str, int] | None = None
    for line in markdown.replace("\r\n", "\n").split("\n"):
        marker = _FENCE.match(line)
        if fence is None and marker is not None:
            opening = marker.group("fence")
            fence = (opening[0], len(opening))
            visible.append("")
            continue
        if fence is not None:
            character, minimum_length = fence
            if re.match(rf"^\s*{re.escape(character)}{{{minimum_length},}}\s*$", line):
                fence = None
            visible.append("")
            continue
        visible.append(line)
    return visible


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    for line_number, line in enumerate(lines, start=1):
        match = _ATX_HEADING.match(line)
        if match is not None:
            headings.append((line_number, len(match.group("marks")), match.group("body")))
    return headings


def _table_cells(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = tuple(cell.strip() for cell in stripped.split("|"))
    return cells if len(cells) >= 2 else None


def _is_table_delimiter(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(_TABLE_DELIMITER_CELL.fullmatch(cell) for cell in cells)


def _is_invalid_table_delimiter(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(_TABLE_DELIMITER_LIKE_CELL.fullmatch(cell) for cell in cells)


def _is_outer_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and _table_cells(line) is not None


def _table_starts(lines: list[str]) -> list[int]:
    return [
        index
        for index in range(len(lines) - 1)
        if _table_cells(lines[index]) and _is_table_delimiter(lines[index + 1])
    ]


def _table_block_lines(lines: list[str]) -> set[int]:
    table_lines: set[int] = set()
    for start in _table_starts(lines):
        index = start
        while index < len(lines) and _table_cells(lines[index]) is not None:
            table_lines.add(index)
            index += 1
    return table_lines


def _starts_block(line: str, *, table_line: bool) -> bool:
    return bool(
        _ATX_HEADING.match(line)
        or _UNORDERED_ITEM.match(line)
        or _ORDERED_STEP.match(line)
        or _BLOCKQUOTE.match(line)
        or _THEMATIC_BREAK.match(line)
        or table_line
    )


def _long_paragraph_lines(lines: list[str]) -> list[int]:
    long_starts: list[int] = []
    paragraph: list[tuple[int, str]] = []
    table_lines = _table_block_lines(lines)

    def finish() -> None:
        if not paragraph:
            return
        text = " ".join(line.strip() for _, line in paragraph)
        first = paragraph[0][1].lstrip()
        if (
            len(text) > _LONG_PARAGRAPH_CHARS
            and not _ATX_HEADING.match(first)
            and not first.startswith(("|", ">", "- ", "* ", "+ "))
            and not _ORDERED_STEP.match(first)
        ):
            long_starts.append(paragraph[0][0])

    for index, line in enumerate(lines):
        line_number = index + 1
        if line.strip():
            if _starts_block(line, table_line=index in table_lines):
                finish()
                paragraph = []
            else:
                paragraph.append((line_number, line))
        else:
            finish()
            paragraph = []
    finish()
    return long_starts


def inspect_card_markdown(markdown: str) -> CardQualitySignals:
    """Extract deterministic presentation signals without judging content semantics."""
    lines = _without_fenced_code(markdown)
    headings = _headings(lines)
    manual_ordinals = [
        body
        for _, level, body in headings
        if level in {2, 3, 4} and _MANUAL_ORDINAL.match(body)
    ]
    table_count = len(_table_starts(lines))
    return CardQualitySignals(
        h2_count=sum(level == 2 for _, level, _ in headings),
        h3_count=sum(level == 3 for _, level, _ in headings),
        table_count=table_count,
        ordered_step_count=sum(bool(_ORDERED_STEP.match(line)) for line in lines),
        blockquote_count=sum(line.lstrip().startswith(">") for line in lines),
        long_paragraph_count=len(_long_paragraph_lines(lines)),
        reporting_tone_spans=[phrase for phrase in _REPORTING_TONE if phrase in markdown],
        manual_ordinal_headings=manual_ordinals,
    )


def _structure_errors(markdown: str) -> list[str]:
    lines = _without_fenced_code(markdown)
    headings = _headings(lines)
    errors: list[str] = []
    h1s = [(line_number, body) for line_number, level, body in headings if level == 1]
    first_nonempty = next(
        (line_number for line_number, line in enumerate(lines, start=1) if line.strip()),
        None,
    )
    if first_nonempty is None or not h1s or h1s[0][0] != first_nonempty:
        location = first_nonempty if first_nonempty is not None else 1
        errors.append(f"missing H1 at line {location}")
    for line_number, _ in h1s[1:]:
        errors.append(f"duplicate H1 at line {line_number}")

    h2s = [(line_number, body) for line_number, level, body in headings if level == 2]
    if not h2s:
        errors.append("missing H2 section")
    elif h1s:
        core_lines = lines[h1s[0][0] : h2s[0][0] - 1]
        core_content = re.sub(r"<!--.*?-->", "", "\n".join(core_lines), flags=re.S)
        if not any(
            line.strip() and not _ATX_HEADING.match(line) and not _THEMATIC_BREAK.match(line)
            for line in core_content.splitlines()
        ):
            errors.append(f"missing core information block before H2 at line {h2s[0][0]}")

    for line_number, level, body in headings:
        if level in {2, 3, 4} and _MANUAL_ORDINAL.match(body):
            errors.append(f"manual ordinal heading at line {line_number}")

    for line_number in _long_paragraph_lines(lines):
        errors.append(f"long paragraph at line {line_number}")

    for index in range(len(lines) - 1):
        if (
            _table_cells(lines[index])
            and (index == 0 or _table_cells(lines[index - 1]) is None)
        ):
            next_line = lines[index + 1]
            if (
                not _is_table_delimiter(next_line)
                and (
                    (_is_outer_table_row(lines[index]) and _is_outer_table_row(next_line))
                    or _is_invalid_table_delimiter(next_line)
                )
            ):
                errors.append(f"malformed table at line {index + 1}")
    return errors


def validate_card_markdown(markdown: str) -> None:
    """Reject only deterministic card-level Markdown contract violations."""
    errors = _structure_errors(markdown)
    if errors:
        raise CardStructureError(errors)


def validate_v1_structure(result) -> None:
    """Apply the card Markdown gate to every card in a unified V1 result."""
    errors: list[str] = []
    for scene in result.scene_results:
        for card_number, card in enumerate(scene.cards, start=1):
            try:
                validate_card_markdown(card.markdown)
            except CardStructureError as error:
                errors.extend(
                    f"{scene.scene_id} card {card_number}: {message}"
                    for message in error.errors
                )
    if errors:
        raise CardStructureError(errors)
