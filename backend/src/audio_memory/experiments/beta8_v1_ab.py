from __future__ import annotations

from hashlib import sha256
import json
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import time
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from audio_memory.prompts.beta8_scene_schema import (
    BETA8_SCENE_IDS,
    Beta8SearchCandidate,
    Beta8TodoCandidate,
    parse_beta8_card_markdown,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


CommunicationKind = Literal[
    "meeting",
    "visit",
    "call",
    "online_conversation",
    "one_on_one",
    "interview",
    "customer_conversation",
    "supplier_conversation",
    "partner_conversation",
    "work_recording",
    "other",
]


class Beta8CardBasis(_StrictModel):
    type: Literal["work_communication", "independent_value"]
    unit_key: str | None = Field(default=None, min_length=1, max_length=200)
    communication_kind: CommunicationKind | None = None

    @model_validator(mode="after")
    def validate_basis(self) -> "Beta8CardBasis":
        if self.type == "work_communication":
            if self.unit_key is None or self.communication_kind is None:
                raise ValueError("work communication basis requires unit_key and kind")
        elif self.unit_key is not None or self.communication_kind is not None:
            raise ValueError("independent value basis cannot claim communication identity")
        return self


class Beta8UnifiedCard(_StrictModel):
    card_basis: Beta8CardBasis
    markdown: str = Field(min_length=1, max_length=120_000)
    source_segment_ids: list[str] = Field(default_factory=list, max_length=4_000)
    search_candidates: list[Beta8SearchCandidate] = Field(default_factory=list, max_length=5)


class Beta8UnifiedSceneResult(_StrictModel):
    scene_id: Literal[
        "work_communication",
        "parenting_family",
        "health_state",
        "content_consumption",
        "inspiration_insight",
        "self_growth",
        "life_decisions",
    ]
    cards: list[Beta8UnifiedCard] = Field(default_factory=list, max_length=80)
    todo_candidates: list[Beta8TodoCandidate] = Field(default_factory=list, max_length=300)
    skip_reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_skip_reason(self) -> "Beta8UnifiedSceneResult":
        if self.cards and self.skip_reason is not None:
            raise ValueError("skip_reason must be null when cards exist")
        if not self.cards and not (self.skip_reason or "").strip():
            raise ValueError("skip_reason is required when cards are empty")
        return self


class Beta8PlanCorrection(_StrictModel):
    brief_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2_000)


class Beta8UnifiedV1Result(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    scene_results: list[Beta8UnifiedSceneResult]
    plan_corrections: list[Beta8PlanCorrection] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_scene_set(self) -> "Beta8UnifiedV1Result":
        if len(self.scene_results) != len(BETA8_SCENE_IDS):
            raise ValueError("scene_results must contain exactly seven scenes")
        actual = tuple(item.scene_id for item in self.scene_results)
        if actual != BETA8_SCENE_IDS:
            raise ValueError("scene_results must use the ordered scene IDs")
        if self.input_complete and self.input_error is not None:
            raise ValueError("complete input cannot have input_error")
        if not self.input_complete and not (self.input_error or "").strip():
            raise ValueError("incomplete input requires input_error")
        return self


class Beta8PlanCardBrief(_StrictModel):
    brief_key: str = Field(min_length=1, max_length=200)
    scene_id: Literal[
        "work_communication",
        "parenting_family",
        "health_state",
        "content_consumption",
        "inspiration_insight",
        "self_growth",
        "life_decisions",
    ]
    card_basis: Beta8CardBasis
    core_value: str = Field(min_length=1, max_length=4_000)
    required_topics: list[str] = Field(default_factory=list, max_length=30)
    attribution_notes: list[str] = Field(default_factory=list, max_length=20)
    source_segment_ids: list[str] = Field(default_factory=list, max_length=80)


class Beta8WritingPlan(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    card_briefs: list[Beta8PlanCardBrief] = Field(default_factory=list, max_length=200)
    scene_skip_reasons: dict[str, str | None]

    @model_validator(mode="after")
    def validate_plan(self) -> "Beta8WritingPlan":
        if set(self.scene_skip_reasons) != set(BETA8_SCENE_IDS):
            raise ValueError("scene_skip_reasons must cover exactly seven scenes")
        keys = [item.brief_key for item in self.card_briefs]
        if len(keys) != len(set(keys)):
            raise ValueError("plan brief_key values must be unique")
        return self


class Beta8EventMapCommunication(_StrictModel):
    unit_key: str = Field(min_length=1, max_length=120)
    communication_kind: CommunicationKind
    purpose: str = Field(min_length=1, max_length=160)
    topics: list[str] = Field(default_factory=list, max_length=12)
    start_segment_id: str = Field(min_length=1, max_length=80)
    end_segment_id: str = Field(min_length=1, max_length=80)
    anchor_segment_ids: list[str] = Field(default_factory=list, max_length=12)
    attribution_note: str | None = Field(default=None, max_length=240)


class Beta8EventMapOtherCandidate(_StrictModel):
    candidate_key: str = Field(min_length=1, max_length=120)
    scene_id: Literal[
        "parenting_family",
        "health_state",
        "content_consumption",
        "inspiration_insight",
        "self_growth",
        "life_decisions",
    ]
    core_value: str = Field(min_length=1, max_length=240)
    anchor_segment_ids: list[str] = Field(default_factory=list, max_length=12)
    attribution_note: str | None = Field(default=None, max_length=240)


class Beta8LightweightEventMap(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    work_communications: list[Beta8EventMapCommunication] = Field(default_factory=list)
    other_candidates: list[Beta8EventMapOtherCandidate] = Field(default_factory=list)
    scene_skip_reasons: dict[str, str | None]

    @model_validator(mode="after")
    def validate_event_map(self) -> "Beta8LightweightEventMap":
        if set(self.scene_skip_reasons) != set(BETA8_SCENE_IDS):
            raise ValueError("scene_skip_reasons must cover exactly seven scenes")
        keys = [item.unit_key for item in self.work_communications]
        keys.extend(item.candidate_key for item in self.other_candidates)
        if len(keys) != len(set(keys)):
            raise ValueError("event map keys must be unique")
        if self.input_complete and self.input_error is not None:
            raise ValueError("complete input cannot have input_error")
        if not self.input_complete and not (self.input_error or "").strip():
            raise ValueError("incomplete input requires input_error")
        return self


def validate_event_map_ids(
    event_map: Beta8LightweightEventMap, *, known_segment_ids: set[str]
) -> None:
    referenced: set[str] = set()
    for item in event_map.work_communications:
        referenced.add(item.start_segment_id)
        referenced.add(item.end_segment_id)
        referenced.update(item.anchor_segment_ids)
    for item in event_map.other_candidates:
        referenced.update(item.anchor_segment_ids)
    unknown = sorted(referenced - known_segment_ids)
    if unknown:
        raise ValueError(f"Unknown event-map segment IDs: {', '.join(unknown)}")


_MANUAL_ORDINAL = re.compile(
    r"^#{2,4}\s+(?:\d+[.)、]|[一二三四五六七八九十]+、|（(?:\d+|[一二三四五六七八九十]+)）)"
)


def validate_unified_v1(
    result: Beta8UnifiedV1Result, *, known_segment_ids: set[str]
) -> None:
    referenced: set[str] = set()
    unit_keys: set[str] = set()
    for scene in result.scene_results:
        for card in scene.cards:
            parse_beta8_card_markdown(card.markdown)
            if any(_MANUAL_ORDINAL.match(line.strip()) for line in card.markdown.splitlines()):
                raise ValueError("Markdown section heading contains a manual ordinal")
            referenced.update(card.source_segment_ids)
            referenced.update(
                segment_id
                for candidate in card.search_candidates
                for segment_id in candidate.related_segment_ids
            )
            basis = card.card_basis
            if basis.type == "work_communication":
                if scene.scene_id != "work_communication":
                    raise ValueError("work communication basis requires work scene")
                assert basis.unit_key is not None
                if basis.unit_key in unit_keys:
                    raise ValueError(
                        f"Duplicate work communication unit_key: {basis.unit_key}"
                    )
                unit_keys.add(basis.unit_key)
        for todo in scene.todo_candidates:
            referenced.update(todo.evidence_segment_ids)
    unknown = sorted(referenced - known_segment_ids)
    if unknown:
        raise ValueError(f"Unknown transcript segment IDs: {', '.join(unknown)}")


class DeterministicReview(_StrictModel):
    card_count: int
    markdown_characters: int
    heading_count: int
    table_count: int
    long_paragraph_count: int
    reporting_tone_flags: list[str]
    duplicate_normalized_titles: list[str]
    semantic_correctness: None = None


_REPORTING_TONE = (
    "这份记录",
    "本次材料",
    "逐字稿中",
    "分析指向",
    "该事件反映出",
)


def _normalized_title(markdown: str) -> str:
    parsed = parse_beta8_card_markdown(markdown)
    return re.sub(r"[\W_]+", "", parsed.title).casefold()


def review_variant(result: Beta8UnifiedV1Result) -> DeterministicReview:
    cards = [card for scene in result.scene_results for card in scene.cards]
    markdown = "\n\n".join(card.markdown for card in cards)
    headings = [
        line for line in markdown.splitlines() if re.match(r"^#{2,4}\s+", line.strip())
    ]
    tables = sum(
        1
        for line in markdown.splitlines()
        if re.match(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+", line)
    )
    paragraphs = re.split(r"\n\s*\n", markdown)
    long_paragraphs = [
        value
        for value in paragraphs
        if len(value.strip()) > 420 and not value.lstrip().startswith(("#", "|", "- "))
    ]
    tone_flags = [phrase for phrase in _REPORTING_TONE if phrase in markdown]
    title_counts: dict[str, int] = {}
    for card in cards:
        title = _normalized_title(card.markdown)
        title_counts[title] = title_counts.get(title, 0) + 1
    return DeterministicReview(
        card_count=len(cards),
        markdown_characters=len(markdown),
        heading_count=len(headings),
        table_count=tables,
        long_paragraph_count=len(long_paragraphs),
        reporting_tone_flags=tone_flags,
        duplicate_normalized_titles=sorted(
            title for title, count in title_counts.items() if count > 1
        ),
    )


def build_blind_map(seed: str) -> dict[str, str]:
    if int(sha256(seed.encode()).hexdigest(), 16) % 2:
        return {"X": "variant-a", "Y": "variant-b"}
    return {"X": "variant-b", "Y": "variant-a"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ExperimentPromptSet:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (
            Path(__file__).resolve().parents[1]
            / "prompts"
            / "beta8"
            / "experiments"
        )

    def read(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")

    def unified_schema_json(self) -> str:
        return _canonical_json(Beta8UnifiedV1Result.model_json_schema())

    def plan_schema_json(self) -> str:
        return _canonical_json(Beta8LightweightEventMap.model_json_schema())

    def _common(self) -> str:
        return "\n\n".join(
            (self.read("shared-quality.md"), self.read("scene-boundaries.md"))
        )

    @staticmethod
    def _transcript_user_data(transcript_markdown: str) -> str:
        safe = transcript_markdown.replace(
            "</beta8_full_transcript>", "<\\/beta8_full_transcript>"
        )
        return (
            '<beta8_full_transcript untrusted="true">'
            f"{safe}</beta8_full_transcript>"
        )

    @staticmethod
    def _with_schema(instructions: str, schema_json: str) -> str:
        return (
            instructions
            + "\n\n运行时 JSON 契约：只返回一个与下列 JSON Schema "
            "完全匹配的对象，不输出代码围栏或额外解释。\n"
            + schema_json
        )

    def compose_variant_a(self, transcript_markdown: str) -> "ExperimentRequest":
        schema = self.unified_schema_json()
        return ExperimentRequest(
            stage="variant-a-write",
            instructions=self._with_schema(
                "\n\n".join((self._common(), self.read("variant-a-unified.md"))),
                schema,
            ),
            user_data=self._transcript_user_data(transcript_markdown),
            schema_json=schema,
            thinking_enabled=True,
        )

    def compose_variant_b_plan(self, transcript_markdown: str) -> "ExperimentRequest":
        schema = self.plan_schema_json()
        return ExperimentRequest(
            stage="variant-b-plan",
            instructions=self._with_schema(
                "\n\n".join((self._common(), self.read("variant-b-plan.md"))),
                schema,
            ),
            user_data=self._transcript_user_data(transcript_markdown),
            schema_json=schema,
            thinking_enabled=False,
        )

    def compose_variant_b_write(
        self,
        transcript_markdown: str,
        plan: Beta8LightweightEventMap | Beta8WritingPlan | dict[str, Any],
    ) -> "ExperimentRequest":
        schema = self.unified_schema_json()
        plan_payload = (
            plan.model_dump(mode="json")
            if isinstance(plan, (Beta8LightweightEventMap, Beta8WritingPlan))
            else plan
        )
        safe_plan = _canonical_json(plan_payload).replace(
            "</beta8_writing_plan>", "<\\/beta8_writing_plan>"
        )
        user_data = (
            f'<beta8_writing_plan untrusted="true">{safe_plan}'
            "</beta8_writing_plan>\n\n"
            + self._transcript_user_data(transcript_markdown)
        )
        return ExperimentRequest(
            stage="variant-b-write",
            instructions=self._with_schema(
                "\n\n".join((self._common(), self.read("variant-b-write.md"))),
                schema,
            ),
            user_data=user_data,
            schema_json=schema,
            thinking_enabled=True,
        )

    def manifest(self) -> dict[str, str]:
        names = (
            "shared-quality.md",
            "scene-boundaries.md",
            "variant-a-unified.md",
            "variant-b-plan.md",
            "variant-b-write.md",
        )
        return {
            name: sha256((self.root / name).read_bytes()).hexdigest() for name in names
        }


@dataclass(frozen=True, slots=True)
class ExperimentRequest:
    stage: str
    instructions: str
    user_data: str
    schema_json: str
    thinking_enabled: bool


def transcript_markdown(rows: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for row in rows:
        attributes = (
            f'id="{html.escape(str(row["segment_id"]), quote=True)}" '
            f'file="{html.escape(str(row["file_name"]), quote=True)}" '
            f'start_ms="{int(row["start_ms"])}" end_ms="{int(row["end_ms"])}"'
        )
        text = str(row["text"]).replace("</segment>", "<\\/segment>")
        parts.append(f"<segment {attributes}>{text}</segment>")
    return "\n".join(parts)


def planning_transcript_markdown(rows: list[dict[str, object]]) -> str:
    parts: list[str] = []
    current_file_position: int | None = None
    for row in rows:
        file_position = int(row["file_position"])
        if file_position != current_file_position:
            if current_file_position is not None:
                parts.append("</file>")
            parts.append(
                f'<file position="{file_position}" '
                f'name="{html.escape(str(row["file_name"]), quote=True)}">'
            )
            current_file_position = file_position
        text = str(row["text"]).replace("</file>", "<\\/file>")
        parts.append(f'[{row["segment_id"]}] {text}')
    if current_file_position is not None:
        parts.append("</file>")
    return "\n".join(parts)


def _parse_json_response(raw: str, result_type: type[BaseModel]) -> BaseModel:
    candidate = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    return result_type.model_validate_json(candidate)


def parse_unified_response(raw: str) -> Beta8UnifiedV1Result:
    candidate = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    payload = json.loads(candidate)
    for scene in payload.get("scene_results", []):
        scene_todos = list(scene.get("todo_candidates") or [])
        for card in scene.get("cards", []):
            scene_todos.extend(card.pop("todo_candidates", []) or [])
            basis = card.get("card_basis") or {}
            if basis.get("type") == "independent_value":
                basis["unit_key"] = None
                basis["communication_kind"] = None
        scene["todo_candidates"] = scene_todos
    return Beta8UnifiedV1Result.model_validate(payload)


def _diagnostic_payload(item: object) -> dict[str, object]:
    fields = (
        "provider_id",
        "model_id",
        "scene_id",
        "parameter_fingerprint",
        "request_bytes",
        "response_bytes",
        "segment_count",
        "input_tokens",
        "output_tokens",
        "elapsed_seconds",
        "status_category",
        "finish_reason",
        "repair_attempted",
    )
    return {name: getattr(item, name) for name in fields if hasattr(item, name)}


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _inline_markdown(value: str) -> str:
    rendered = html.escape(value, quote=True)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    return rendered


def _render_card_markdown(markdown: str) -> str:
    lines = markdown.splitlines()
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            level = min(len(heading.group(1)) + 1, 5)
            rendered.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
            index += 1
            continue
        if "|" in line and index + 1 < len(lines) and re.match(
            r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$", lines[index + 1]
        ):
            headers = [cell.strip() for cell in line.strip("|").split("|")]
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index]:
                rows.append(
                    [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                )
                index += 1
            table = "<table><thead><tr>" + "".join(
                f"<th>{_inline_markdown(cell)}</th>" for cell in headers
            ) + "</tr></thead><tbody>"
            table += "".join(
                "<tr>" + "".join(f"<td>{_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            rendered.append(table + "</tbody></table>")
            continue
        unordered = re.match(r"^[-*]\s+(.+)$", line)
        ordered = re.match(r"^\d+[.)]\s+(.+)$", line)
        if unordered or ordered:
            tag = "ul" if unordered else "ol"
            items: list[str] = []
            while index < len(lines):
                current = lines[index].strip()
                match = (
                    re.match(r"^[-*]\s+(.+)$", current)
                    if tag == "ul"
                    else re.match(r"^\d+[.)]\s+(.+)$", current)
                )
                if match is None:
                    break
                items.append(f"<li>{_inline_markdown(match.group(1))}</li>")
                index += 1
            rendered.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            following = lines[index].strip()
            if re.match(r"^(?:#{1,4}\s+|[-*]\s+|\d+[.)]\s+)", following):
                break
            if "|" in following and index + 1 < len(lines) and re.match(
                r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$", lines[index + 1]
            ):
                break
            paragraph.append(following)
            index += 1
        rendered.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
    return "\n".join(rendered)


_SCENE_LABELS = {
    "work_communication": "工作沟通",
    "parenting_family": "亲子家庭",
    "health_state": "健康状态",
    "content_consumption": "内容消费",
    "inspiration_insight": "灵感洞察",
    "self_growth": "自我成长",
    "life_decisions": "生活决策",
}


def _render_variant(label: str, result: Beta8UnifiedV1Result) -> str:
    cards: list[str] = []
    for scene in result.scene_results:
        for card in scene.cards:
            cards.append(
                '<article class="card">'
                f'<div class="tag">{_SCENE_LABELS[scene.scene_id]}</div>'
                f'{_render_card_markdown(card.markdown)}'
                "</article>"
            )
    return (
        f'<section class="variant" data-label="{label}">'
        f'<div class="variant-head"><h1>方案 {label}</h1>'
        f'<span>{len(cards)} 张卡片</span></div>{"".join(cards)}</section>'
    )


def render_blind_comparison(
    *, blind_map: dict[str, str], results: dict[str, Beta8UnifiedV1Result]
) -> str:
    sections = "".join(
        _render_variant(label, results[blind_map[label]]) for label in ("X", "Y")
    )
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Beta 8 V1 盲评</title>
<style>
:root{{--paper:#f3f0e9;--card:#fffdf8;--ink:#181a17;--muted:#676b63;--line:#d8d4ca;--accent:#205c46}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;line-height:1.72}}
.top{{position:sticky;top:0;z-index:2;background:rgba(243,240,233,.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:14px 24px;display:flex;gap:10px;align-items:center}}
.top strong{{margin-right:auto}}button{{border:1px solid var(--line);background:white;border-radius:999px;padding:8px 18px;font-size:14px;cursor:pointer}}button.active{{background:var(--accent);color:white;border-color:var(--accent)}}
.wrap{{max-width:900px;margin:0 auto;padding:42px 22px 100px}}.intro{{margin-bottom:32px}}.intro h2{{font:650 38px/1.2 ui-serif,"Songti SC",serif;margin:0 0 12px}}.intro p{{color:var(--muted)}}.variant{{display:none}}.variant.active{{display:block}}.variant-head{{display:flex;justify-content:space-between;align-items:end;margin-bottom:18px}}.variant-head h1{{font:650 30px ui-serif,"Songti SC",serif;margin:0}}.variant-head span{{color:var(--muted)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:34px 38px;margin:22px 0;box-shadow:0 10px 30px rgba(20,25,20,.035)}}.tag{{display:inline-block;color:var(--accent);background:#e5eee8;border-radius:99px;padding:3px 10px;font-size:12px;font-weight:700;margin-bottom:18px}}h2{{font:650 29px/1.3 ui-serif,"Songti SC",serif;margin:0 0 18px}}h3{{font:650 20px/1.4 ui-serif,"Songti SC",serif;margin:30px 0 10px}}h4{{font-size:16px;margin:24px 0 8px}}p{{margin:0 0 14px}}ul,ol{{padding-left:1.4em}}li{{margin:5px 0}}table{{border-collapse:collapse;width:100%;margin:14px 0 20px;font-size:14px}}th,td{{border:1px solid var(--line);padding:9px 11px;text-align:left;vertical-align:top}}th{{background:#eeece5}}code{{background:#efede7;padding:2px 5px;border-radius:5px}}
@media(max-width:650px){{.card{{padding:25px 20px}}.intro h2{{font-size:31px}}}}
</style></head><body><div class="top"><strong>Audio Memory · V1 架构盲评</strong><button data-target="X" class="active">方案 X</button><button data-target="Y">方案 Y</button></div><main class="wrap"><div class="intro"><h2>只看报告质量，不看生成方式</h2><p>两份报告使用同一份逐字稿、同一模型和同一套质量规则。请重点比较：工作沟通是否按沟通事件聚合、主体归属、卡片价值、内容丰富度和结构可读性。</p></div>{sections}</main>
<script>const buttons=[...document.querySelectorAll('button')],sections=[...document.querySelectorAll('.variant')];function show(v){{buttons.forEach(b=>b.classList.toggle('active',b.dataset.target===v));sections.forEach(s=>s.classList.toggle('active',s.dataset.label===v));}}buttons.forEach(b=>b.onclick=()=>show(b.dataset.target));show('X');</script></body></html>'''


class Beta8V1ABRunner:
    def __init__(
        self,
        provider: object,
        *,
        prompts: ExperimentPromptSet | None = None,
        provider_id: str = "deepseek",
        model_id: str = "deepseek-v4-pro",
        max_tokens: int = 32_000,
        timeout_seconds: float = 900.0,
    ) -> None:
        self.provider = provider
        self.prompts = prompts or ExperimentPromptSet()
        self.provider_id = provider_id
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    async def _call(self, request: ExperimentRequest, *, segment_count: int) -> str:
        return await self.provider.generate(
            self.provider_id,
            system=request.instructions,
            user=request.user_data,
            model_id=self.model_id,
            scene_id=f"beta8-v1-ab-{request.stage}",
            max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds,
            segment_count=segment_count,
            thinking_enabled=request.thinking_enabled,
        )

    async def run(
        self,
        *,
        rows: list[dict[str, object]],
        output: Path,
        source: Path | None = None,
        resume_variant_a: bool = False,
        resumed_variant_a_elapsed_seconds: float | None = None,
        resume_variant_b: bool = False,
    ) -> dict[str, dict[str, object]]:
        output.mkdir(parents=True, exist_ok=True)
        for name in ("variant-a", "variant-b"):
            (output / name / "cards").mkdir(parents=True, exist_ok=True)
        transcript = transcript_markdown(rows)
        planning_transcript = planning_transcript_markdown(rows)
        known_ids = {str(row["segment_id"]) for row in rows}
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "prompt_hashes": self.prompts.manifest(),
            "source": str(source) if source else None,
            "source_sha256": sha256(source.read_bytes()).hexdigest() if source else None,
            "segment_count": len(rows),
            "transcript_characters": sum(len(str(row["text"])) for row in rows),
        }
        _write_json(output / "experiment-manifest.json", manifest)

        diagnostic_start = len(getattr(self.provider, "request_diagnostics", []))
        if resume_variant_a:
            raw_a = (output / "variant-a" / "raw.txt").read_text(encoding="utf-8")
            elapsed_a = resumed_variant_a_elapsed_seconds
        else:
            variant_a_started = time.monotonic()
            raw_a = await self._call(
                self.prompts.compose_variant_a(transcript), segment_count=len(rows)
            )
            elapsed_a = time.monotonic() - variant_a_started
            (output / "variant-a" / "raw.txt").write_text(raw_a, encoding="utf-8")
            _write_json(
                output / "variant-a" / "stage-diagnostics.json",
                [
                    _diagnostic_payload(item)
                    for item in getattr(self.provider, "request_diagnostics", [])[diagnostic_start:]
                ],
            )
        result_a = parse_unified_response(raw_a)
        validate_unified_v1(result_a, known_segment_ids=known_ids)
        after_a = len(getattr(self.provider, "request_diagnostics", []))

        if resume_variant_b:
            plan = Beta8LightweightEventMap.model_validate_json(
                (output / "variant-b" / "plan.json").read_text(encoding="utf-8")
            )
            validate_event_map_ids(plan, known_segment_ids=known_ids)
            raw_b = (output / "variant-b" / "raw.txt").read_text(encoding="utf-8")
            diagnostics_path = output / "variant-b" / "all-stage-diagnostics.json"
            saved_b_diagnostics = (
                json.loads(diagnostics_path.read_text(encoding="utf-8"))
                if diagnostics_path.exists()
                else []
            )
            elapsed_b = sum(
                float(item.get("elapsed_seconds", 0)) for item in saved_b_diagnostics
            )
        else:
            variant_b_started = time.monotonic()
            try:
                raw_plan = await self._call(
                    self.prompts.compose_variant_b_plan(planning_transcript),
                    segment_count=len(rows),
                )
            finally:
                _write_json(
                    output / "variant-b" / "plan-stage-diagnostics.json",
                    [
                        _diagnostic_payload(item)
                        for item in getattr(self.provider, "request_diagnostics", [])[after_a:]
                    ],
                )
            (output / "variant-b" / "plan-raw.txt").write_text(
                raw_plan, encoding="utf-8"
            )
            plan = _parse_json_response(raw_plan, Beta8LightweightEventMap)
            assert isinstance(plan, Beta8LightweightEventMap)
            validate_event_map_ids(plan, known_segment_ids=known_ids)
            _write_json(output / "variant-b" / "plan.json", plan.model_dump(mode="json"))
            raw_b = await self._call(
                self.prompts.compose_variant_b_write(transcript, plan),
                segment_count=len(rows),
            )
            _write_json(
                output / "variant-b" / "all-stage-diagnostics.json",
                [
                    _diagnostic_payload(item)
                    for item in getattr(self.provider, "request_diagnostics", [])[after_a:]
                ],
            )
            elapsed_b = time.monotonic() - variant_b_started
            (output / "variant-b" / "raw.txt").write_text(raw_b, encoding="utf-8")
            saved_b_diagnostics = []
        result_b = parse_unified_response(raw_b)
        validate_unified_v1(result_b, known_segment_ids=known_ids)
        after_b = len(getattr(self.provider, "request_diagnostics", []))

        results = {"variant-a": result_a, "variant-b": result_b}
        diagnostics = [
            _diagnostic_payload(item)
            for item in getattr(self.provider, "request_diagnostics", [])[diagnostic_start:]
        ]
        slices = {
            "variant-a": diagnostics[: after_a - diagnostic_start],
            "variant-b": (
                saved_b_diagnostics
                if resume_variant_b
                else diagnostics[after_a - diagnostic_start : after_b - diagnostic_start]
            ),
        }
        summary: dict[str, dict[str, object]] = {}
        for variant, elapsed in (("variant-a", elapsed_a), ("variant-b", elapsed_b)):
            result = results[variant]
            variant_dir = output / variant
            _write_json(variant_dir / "result.json", result.model_dump(mode="json"))
            review = review_variant(result)
            _write_json(variant_dir / "deterministic-review.json", review.model_dump(mode="json"))
            card_position = 0
            for scene in result.scene_results:
                for card in scene.cards:
                    card_position += 1
                    (variant_dir / "cards" / f"{card_position:02d}-{scene.scene_id}.md").write_text(
                        card.markdown, encoding="utf-8"
                    )
            variant_diagnostics = slices[variant]
            is_resumed_a = variant == "variant-a" and resume_variant_a
            is_resumed_b = variant == "variant-b" and resume_variant_b
            summary[variant] = {
                "call_count": 1 if is_resumed_a else len(variant_diagnostics),
                "elapsed_seconds": (
                    round(elapsed, 3) if elapsed is not None else None
                ),
                "input_tokens": (
                    None if is_resumed_a else sum(int(item.get("input_tokens", 0)) for item in variant_diagnostics)
                ),
                "output_tokens": (
                    None if is_resumed_a else sum(int(item.get("output_tokens", 0)) for item in variant_diagnostics)
                ),
                "card_count": review.card_count,
                "diagnostics": variant_diagnostics,
                "resumed_from_saved_raw": is_resumed_a,
                "materialized_from_saved_raw": is_resumed_b,
            }
            _write_json(variant_dir / "metrics.json", summary[variant])
        _write_json(output / "metrics-summary.json", summary)
        blind_map = build_blind_map(str(manifest["source_sha256"] or sha256(transcript.encode()).hexdigest()))
        _write_json(output / "blind-map.json", blind_map)
        (output / "comparison.html").write_text(
            render_blind_comparison(blind_map=blind_map, results=results),
            encoding="utf-8",
        )
        return summary
