from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from audio_memory.prompts.autonomous_schema import (
    AutonomousAnalysisResult,
    AutonomousCard,
    AutonomousQuote,
    AutonomousRecommendation,
    AutonomousSection,
)


class ExternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExternalEvidence(ExternalModel):
    evidence_segment_ids: list[str] = Field(default_factory=list)


class ExternalFinding(ExternalEvidence):
    type: Literal["fact", "inference", "pattern", "strength", "risk", "uncertainty"]
    content: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]


class ExternalAnalysis(ExternalEvidence):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)


class ExternalQuote(ExternalEvidence):
    quote: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)


class ExternalAction(ExternalModel):
    title: str = Field(min_length=1)
    why: str = Field(min_length=1)
    steps: list[str] = Field(default_factory=list)
    suggested_language: str | None = None
    success_signal: str | None = None
    caveat: str | None = None


class ExternalTimeRange(ExternalModel):
    start: str | int | None = None
    end: str | int | None = None


class ExternalCard(ExternalModel):
    card_kind: Literal["event", "insight"]
    scene_types: list[str] = Field(default_factory=list)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    time_range: ExternalTimeRange
    findings: list[ExternalFinding] = Field(default_factory=list)
    analysis: list[ExternalAnalysis] = Field(default_factory=list)
    quotes: list[ExternalQuote] = Field(default_factory=list)
    actions: list[ExternalAction] = Field(default_factory=list)


class ExternalEnvelope(ExternalModel):
    status: str
    cards: list[ExternalCard]


def _evidence_union(card: ExternalCard) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    evidenced = [*card.findings, *card.analysis, *card.quotes]
    for item in evidenced:
        for segment_id in item.evidence_segment_ids:
            if segment_id not in seen:
                seen.add(segment_id)
                ordered.append(segment_id)
    return ordered


def _validate_evidence(card: ExternalCard, transcript: dict[str, str]) -> None:
    for item in [*card.findings, *card.analysis, *card.quotes]:
        unknown = [
            segment_id
            for segment_id in item.evidence_segment_ids
            if segment_id not in transcript
        ]
        if unknown:
            raise ValueError(f"unknown evidence segment: {unknown[0]}")
    for quote in card.quotes:
        if not any(
            quote.quote in transcript[segment_id]
            for segment_id in quote.evidence_segment_ids
        ):
            raise ValueError("quote must be verbatim within its evidence segment")


def convert_external_analysis(
    payload: dict[str, object], transcript: dict[str, str]
) -> AutonomousAnalysisResult:
    envelope = ExternalEnvelope.model_validate(payload)
    if envelope.status != "complete":
        raise ValueError("external analysis status must be complete")
    if not envelope.cards:
        raise ValueError("external analysis must contain at least one card")

    converted: list[AutonomousCard] = []
    for source in envelope.cards:
        _validate_evidence(source, transcript)
        metadata = json.dumps(
            {"card_kind": source.card_kind, "scene_types": source.scene_types},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        content = [
            AutonomousSection(
                type="external_meta",
                title="分析类型",
                body=metadata,
            )
        ]
        content.extend(
            AutonomousSection(
                type=f"finding:{finding.type}:{finding.confidence}",
                title="关键发现",
                body=finding.content,
                evidence_segment_ids=finding.evidence_segment_ids,
            )
            for finding in source.findings
        )
        content.extend(
            AutonomousSection(
                type="analysis",
                title=item.title,
                body=item.content,
                evidence_segment_ids=item.evidence_segment_ids,
            )
            for item in source.analysis
        )
        converted.append(
            AutonomousCard(
                title=source.title,
                summary=source.summary,
                content=content,
                quotes=[
                    AutonomousQuote(
                        quote=item.quote,
                        context="",
                        analysis=item.why_it_matters,
                        evidence_segment_ids=item.evidence_segment_ids,
                    )
                    for item in source.quotes
                ],
                recommendations=[
                    AutonomousRecommendation(
                        title=item.title,
                        reason=item.why,
                        actions=item.steps,
                        suggested_language=item.suggested_language,
                        success_signal=item.success_signal,
                        caveat=item.caveat,
                    )
                    for item in source.actions
                ],
                evidence_segment_ids=_evidence_union(source),
            )
        )
    return AutonomousAnalysisResult(cards=converted)
