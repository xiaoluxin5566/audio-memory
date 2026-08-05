from __future__ import annotations

from dataclasses import dataclass

from audio_memory.diarization.engine import SpeakerTurn


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Word timestamps must be increasing")


@dataclass(frozen=True, slots=True)
class AlignedTranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    words: tuple[Word, ...]
    speaker_id: str | None


def assign_speakers(
    words: list[Word], turns: list[SpeakerTurn]
) -> list[AlignedTranscriptSegment]:
    aligned: list[AlignedTranscriptSegment] = []
    for word in words:
        speaker_id: str | None = None
        largest_overlap = 0
        for turn in turns:
            overlap = max(
                0,
                min(word.end_ms, turn.end_ms) - max(word.start_ms, turn.start_ms),
            )
            if overlap > largest_overlap:
                largest_overlap = overlap
                speaker_id = turn.speaker_id
        if aligned and aligned[-1].speaker_id == speaker_id:
            previous = aligned[-1]
            aligned[-1] = AlignedTranscriptSegment(
                start_ms=previous.start_ms,
                end_ms=word.end_ms,
                text=previous.text + word.text,
                words=previous.words + (word,),
                speaker_id=speaker_id,
            )
            continue
        aligned.append(
            AlignedTranscriptSegment(
                start_ms=word.start_ms,
                end_ms=word.end_ms,
                text=word.text,
                words=(word,),
                speaker_id=speaker_id,
            )
        )
    return aligned
