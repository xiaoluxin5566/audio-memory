from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import Segment
from .sources import digest

TIME = r"\d{2,}:\d{2}:\d{2}"
LINE = re.compile(
    rf"^\[({TIME})\s*[-–—]\s*({TIME})\]\s*(speaker-\d+)(?:｜情绪：([^：]+))?：(.*)$"
)
META = re.compile(r"^-\s*([^：]+)：(.*)$")
LABELLED_LINE = re.compile(
    rf"^\[({TIME})\s*[-–—]\s*({TIME})\]\s*\*\*"
    r"(说话人\d+(?:（[^（）]+）)?|我（声纹匹配）|无法确定)\*\*：(.*)$"
)
LABELLED_METADATA = {"原音频时长", "引擎", "本次结果", "本人样本自匹配对照"}
# Export provenance warnings are retained in the immutable raw artifact, not speech.
LABELLED_NOTICES = {
    "- “声纹匹配本人”是厂商自动匹配结果，待你核验；未匹配不等于已证实是别人。",
    "- “未匹配本人”只表示返回结果没有命中标记，不代表确认是他人。",
    "- 未返回声纹分数则不展示置信度；本次没有人工身份真值。",
}
RETRIED_NOTICE = (
    "原超长录音的重提分段已按原始录音位置还原时间戳；"
    "说话人标签由 ASR 自动聚类，不代表已确认的真实身份。"
)
EXCLUDED_AUDIO_NOTICE = re.compile(
    r"^> 注：`[^`\r\n]+` 已提交至豆包，但服务返回“音频无效”，"
    r"因此本日文档仅包含其余 \d+ 份成功转写的音频。$"
)


def milliseconds(value):
    hours, minutes, seconds = map(int, value.split(":"))
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid_timestamp")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


@dataclass(frozen=True)
class ParsedSource:
    title: str
    declared_date: str
    duration_ms: int | None
    segments: list[Segment]
    section_key: str


@dataclass(frozen=True)
class ParsedTranscript:
    sources: list[ParsedSource]
    coverage_complete: bool
    parser_version: str = "standard-md-v1"


def make_segments(rows, key, duration):
    segments = []
    for ordinal, (
        start,
        end,
        speaker,
        emotion,
        content,
        line_number,
        label,
    ) in enumerate(rows):
        a, b = milliseconds(start), milliseconds(end)
        if b < a or (duration is not None and b > duration):
            raise ValueError("invalid_segment_range")
        if not content.strip():
            raise ValueError("empty_transcript_segment")
        locator = {"line": line_number, "emotion": emotion, "zero_duration": a == b}
        if label is not None:
            locator.update(speaker_label=label, speaker_identity_status="unverified")
        segments.append(
            Segment(
                uid=f"{key[:16]}:{ordinal}",
                ordinal=ordinal,
                text=content,
                start_ms=a,
                end_ms=b,
                speaker_local_id=f"{key}:{speaker}",
                text_locator=locator,
            )
        )
    return segments


class TranscriptParser:
    def parse(self, raw_content, origin_format):
        if origin_format not in {"markdown", "feishu"}:
            raise ValueError("unsupported_transcript_format")
        text = (
            raw_content.decode("utf-8-sig")
            if isinstance(raw_content, bytes)
            else raw_content
        )
        if (
            text.lstrip().startswith("# 豆包逐字稿｜")
            and f"- 说明：{RETRIED_NOTICE}" in text.splitlines()
        ):
            return self._retried_daily(text)
        sections = []
        title = None
        metadata = {}
        rows = []
        body = []
        labelled = False
        has_labelled = False
        has_standard = False
        document_date = ""

        def finish():
            nonlocal has_labelled, has_standard
            duration_key = "原音频时长" if labelled else "音频时长"
            required = {duration_key} if labelled else {"音频时长", "日期", "片段数"}
            if not metadata.get(duration_key) and not metadata.get("片段数"):
                if rows or body:
                    raise ValueError("missing_section_metadata")
                return
            if not title or not required <= metadata.keys():
                raise ValueError("missing_section_metadata")
            try:
                count = int(metadata.get("片段数", len(rows)))
            except ValueError:
                raise ValueError("invalid_segment_count")
            if count < 0 or count != len(rows):
                raise ValueError("segment_count_mismatch")
            if body:
                raise ValueError("unrecognized_transcript_body")
            duration = milliseconds(metadata[duration_key])
            key = digest([len(sections), title])
            segments = make_segments(rows, key, duration)
            sections.append(
                ParsedSource(
                    title=title,
                    declared_date=metadata.get("日期", ""),
                    duration_ms=duration,
                    segments=segments,
                    section_key=key,
                )
            )
            has_labelled |= labelled
            has_standard |= not labelled

        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped == "---":
                continue
            if stripped.startswith("#"):
                finish()
                if (
                    not rows
                    and not {"音频时长", "原音频时长", "片段数"} & metadata.keys()
                ):
                    document_date = metadata.get("日期", document_date)
                title = stripped.lstrip("#").strip()
                labelled = title.endswith("｜完整声纹标记逐字稿")
                metadata = {"日期": document_date} if document_date else {}
                rows = []
                body = []
                continue
            if (
                not rows
                and metadata.get("日期")
                and not {"音频时长", "原音频时长", "片段数"} & metadata.keys()
                and EXCLUDED_AUDIO_NOTICE.fullmatch(stripped)
            ):
                continue
            match = META.match(stripped)
            if labelled and not rows:
                if match and match[1] in LABELLED_METADATA:
                    metadata[match[1]] = match[2].strip()
                    continue
                if stripped in LABELLED_NOTICES:
                    continue
            if match and match[1] in {"日期", "音频时长", "转写引擎", "片段数", "说明"}:
                metadata[match[1]] = match[2].strip()
                continue
            match = LABELLED_LINE.match(stripped) if labelled else LINE.match(stripped)
            if match:
                if labelled:
                    start, end, label, content = match.groups()
                    numeric = re.match(r"说话人(\d+)", label)
                    speaker = (
                        "speaker-" + numeric[1]
                        if numeric
                        else "claimed-owner"
                        if label == "我（声纹匹配）"
                        else "speaker-unknown"
                    )
                    rows.append((start, end, speaker, None, content, number, label))
                else:
                    rows.append((*match.groups(), number, None))
            else:
                body.append((number, line))
        finish()
        if not sections:
            raise ValueError("standard_transcript_required")
        version = (
            "standard-and-speaker-labelled-md-v1"
            if has_labelled and has_standard
            else "speaker-labelled-md-v1"
            if has_labelled
            else "standard-md-v1"
        )
        return ParsedTranscript(
            sources=sections, coverage_complete=True, parser_version=version
        )

    def _retried_daily(self, text):
        """Historical daily export: total count, per-recording headings, no duration."""
        metadata = {}
        sources = []
        rows = []
        title = None

        def finish():
            if title is None:
                return
            key = digest([len(sources), title])
            sources.append(
                ParsedSource(
                    title=title,
                    declared_date=metadata.get("日期", ""),
                    duration_ms=None,
                    segments=make_segments(rows, key, None),
                    section_key=key,
                )
            )

        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            if number == 1 and stripped.startswith("# 豆包逐字稿｜"):
                continue
            if stripped.startswith("## "):
                finish()
                title = stripped[3:].strip()
                if not title:
                    raise ValueError("missing_section_metadata")
                rows = []
                continue
            match = META.match(stripped)
            if (
                title is None
                and match
                and match[1] in {"日期", "转写引擎", "片段数", "说明"}
            ):
                metadata[match[1]] = match[2].strip()
                continue
            match = LINE.match(stripped)
            if title is not None and match:
                rows.append((*match.groups(), number, None))
            else:
                raise ValueError("unrecognized_transcript_body")
        finish()
        if not sources or not {"日期", "转写引擎", "片段数", "说明"} <= metadata.keys():
            raise ValueError("missing_section_metadata")
        try:
            count = int(metadata["片段数"])
        except ValueError:
            raise ValueError("invalid_segment_count")
        if count < 0 or count != sum(len(source.segments) for source in sources):
            raise ValueError("segment_count_mismatch")
        return ParsedTranscript(
            sources, coverage_complete=True, parser_version="retried-daily-md-v1"
        )
