from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_SEARCH_QUERIES = 5
MAX_SEARCH_ROUNDS = 5


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchOverview(StrictModel):
    """The one visible, batch-level overview produced for an upload."""

    title: Literal["本次概览"] = "本次概览"
    summary: str = Field(min_length=1, max_length=4_000)
    scene_ids: list[str] = Field(default_factory=list)

    @field_validator("scene_ids")
    @classmethod
    def validate_unique_scene_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("scene_ids must be unique")
        return value


class AutonomousScene(StrictModel):
    """A model-discovered unit of the recording, with no fixed taxonomy."""

    scene_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=4_000)
    evidence_segment_ids: list[str] = Field(min_length=1)
    file_ids: list[str] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    recommend_deep_analysis: bool
    recommendation_reason: str = Field(min_length=1, max_length=2_000)
    external_verification_need: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_scope(self) -> AutonomousScene:
        if self.end_ms <= self.start_ms:
            raise ValueError("scene end_ms must be greater than start_ms")
        for name, values in (
            ("evidence_segment_ids", self.evidence_segment_ids),
            ("file_ids", self.file_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")
        return self


class NativeSearchQuery(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=1_000)


class NativeSearchDecision(StrictModel):
    action: Literal["search", "finalize"]
    rationale: str = Field(min_length=1, max_length=2_000)
    queries: list[NativeSearchQuery] = Field(default_factory=list, max_length=MAX_SEARCH_QUERIES)

    @model_validator(mode="after")
    def validate_action_queries(self) -> NativeSearchDecision:
        if self.action == "search" and not self.queries:
            raise ValueError("search action requires at least one query")
        if self.action == "finalize" and self.queries:
            raise ValueError("finalize action must not include queries")
        return self


class AutonomousDayMap(StrictModel):
    overview: BatchOverview
    scenes: list[AutonomousScene]
    search_action: NativeSearchDecision

    @model_validator(mode="after")
    def validate_scene_references(self) -> AutonomousDayMap:
        scene_ids = [scene.scene_id for scene in self.scenes]
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError("scene_id values must be unique")
        unknown_scene_ids = set(self.overview.scene_ids) - set(scene_ids)
        if unknown_scene_ids:
            raise ValueError("overview scene_ids must reference scenes in the day map")
        return self


class SearchResultItem(StrictModel):
    """An untrusted but identifiable raw result returned by a provider tool."""

    provider_result_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=1_000)
    url: str = Field(min_length=1, max_length=4_000)
    publisher: str | None = Field(default=None, max_length=500)
    published_at: str | None = Field(default=None, max_length=100)
    snippet: str | None = Field(default=None, max_length=12_000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP(S) URL")
        return value


class ExternalSource(StrictModel):
    """A persisted, user-citable source tied to an actual provider result."""

    source_id: str = Field(min_length=1, max_length=600)
    provider_id: str = Field(min_length=1, max_length=160)
    provider_result_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=1_000)
    url: str = Field(min_length=1, max_length=4_000)
    publisher: str | None = Field(default=None, max_length=500)
    published_at: str | None = Field(default=None, max_length=100)
    support_statement: str | None = Field(default=None, max_length=12_000)
    search_round: int = Field(ge=1, le=MAX_SEARCH_ROUNDS)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return SearchResultItem.validate_url(value)


class SearchRound(StrictModel):
    round_number: int = Field(ge=1, le=MAX_SEARCH_ROUNDS)
    decision: NativeSearchDecision
    results: list[SearchResultItem] = Field(default_factory=list)
    sources: list[ExternalSource] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sources(self) -> SearchRound:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id values must be unique within a search round")
        if any(source.search_round != self.round_number for source in self.sources):
            raise ValueError("sources must retain their originating search round")
        results_by_id = {
            result.provider_result_id: result for result in self.results
        }
        for source in self.sources:
            result = results_by_id.get(source.provider_result_id)
            if result is None:
                raise ValueError(
                    "each source must map to a provider result in the same round"
                )
            if source.title != result.title or source.url != result.url:
                raise ValueError(
                    "sources must retain the title and URL of their provider result"
                )
        return self
