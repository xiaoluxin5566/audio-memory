from __future__ import annotations

from collections.abc import Sequence


def _clock(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds // 1_000)
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_full_transcript_markdown(
    transcript: Sequence[dict[str, object]],
) -> str:
    if not transcript:
        raise ValueError("Analysis requires a completed transcript")
    groups: list[tuple[str, list[dict[str, object]]]] = []
    indexes: dict[str, int] = {}
    for segment in transcript:
        file_id = str(segment.get("file_id") or "unknown-file")
        if file_id not in indexes:
            indexes[file_id] = len(groups)
            groups.append((file_id, []))
        groups[indexes[file_id]][1].append(segment)
    lines = [
        "# 全天录音逐字稿",
        "",
        "> 说明：说话人标签只用于区分声音，不代表任何标签必然是你。请结合上下文判断；不确定时使用概率表达。",
    ]
    for position, (_, segments) in enumerate(groups, start=1):
        first = segments[0]
        lines.extend(["", f"## 文件 {position}：{first.get('file_name') or '未命名音频'}"])
        lines.append(f"录制时间：{first.get('recording_started_at') or '未知'}")
        if first.get("timezone"):
            lines.append(f"时区：{first['timezone']}")
        for segment in segments:
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            start_ms = int(segment.get("start_ms") or 0)
            end_ms = int(segment.get("end_ms") or start_ms)
            lines.extend([
                "",
                f"[{segment.get('segment_id') or 'unknown-segment'}｜{_clock(start_ms)}–{_clock(end_ms)}｜{segment.get('speaker_id') or 'unknown'}]",
                text,
            ])
    return "\n".join(lines).strip() + "\n"
