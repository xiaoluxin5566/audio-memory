from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from audio_memory.transcription.segments import TranscriptSegment


class AsrResultError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CloudTranscriptSegment(TranscriptSegment):
    speaker_id: str | None = None


def normalize_volcano_result(
    *, file_id: str, duration_ms: int, payload: dict[str, Any]
) -> list[CloudTranscriptSegment]:
    try:
        result = payload["result"]
        utterances = result["utterances"]
    except (KeyError, TypeError) as exc:
        raise AsrResultError("invalid result schema") from exc
    if not isinstance(utterances, list):
        raise AsrResultError("invalid utterance list")
    normalized: list[CloudTranscriptSegment] = []
    previous_start = -1
    for index, value in enumerate(utterances):
        if not isinstance(value, dict):
            raise AsrResultError("invalid utterance")
        start_ms = _integer(value.get("start_time"))
        end_ms = _integer(value.get("end_time"))
        text = value.get("text")
        if (
            start_ms < previous_start
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > duration_ms
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise AsrResultError("invalid utterance timeline")
        previous_start = start_ms
        words = _words(value.get("words", []), start_ms=start_ms, end_ms=end_ms)
        speaker = value.get("speaker_id")
        speaker_id = None if speaker is None else f"speaker-{speaker}"
        normalized.append(
            CloudTranscriptSegment(
                file_id=file_id,
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text.strip(),
                words=words,
                speaker_id=speaker_id,
            )
        )
    return normalized


def _words(value: object, *, start_ms: int, end_ms: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AsrResultError("invalid words")
    normalized: list[dict[str, object]] = []
    for word in value:
        if not isinstance(word, dict):
            raise AsrResultError("invalid word")
        word_start = _integer(word.get("start_time"))
        word_end = _integer(word.get("end_time"))
        text = word.get("text")
        if word_start == -1 and word_end == -1:
            continue
        if (
            word_start < start_ms
            or word_end > end_ms
            or word_end <= word_start
            or not isinstance(text, str)
            or not text
        ):
            raise AsrResultError("invalid word timeline")
        normalized.append(
            {"start_ms": word_start, "end_ms": word_end, "text": text}
        )
    return normalized


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AsrResultError("timestamp must be an integer")
    return value
