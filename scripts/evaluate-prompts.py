#!/usr/bin/env python3
"""Deterministic, offline release gate for stored Prompt evaluation examples."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field as PydanticField,
    ValidationError,
    model_validator,
)

from audio_memory.prompts.event_schema import (
    EventMap,
    EventMapDraft,
    StructuredTranscriptSegment,
)
from audio_memory.prompts.evidence import (
    EvidenceIntegrityError,
    validate_evidence_integrity,
)
from audio_memory.prompts.schemas import StrictSceneResult
from audio_memory.prompts.store import PROMPT_SCENES


_MEDIA_EVENT_TYPES = {
    "media",
    "video",
    "youtube_video",
    "tiktok",
    "douyin",
    "live_stream",
    "livestream",
    "launch_event",
    "podcast",
    "music",
    "audiobook",
    "interview",
    "book",
    "course",
    "speech",
    "news",
    "program",
    "song",
}
_TODO_CAPABLE_EVENT_TYPES = {
    "conversation",
    "casual_chat",
    "meeting",
    "work_meeting",
    "parenting",
    "family_interaction",
    "commitment",
    "monologue",
    "phone_call",
    "discussion",
    "work_session",
}
_REQUIRED_COVERAGE = {
    "two_meetings",
    "one_event_multiple_scenes",
    "unrelated_content_events",
    "parenting_interactions",
    "other_person_todo",
    "media_call_to_action",
    "vague_title",
    "high_impact_growth_exception",
    "lightweight_inspiration_phrase",
    "prompt_injection",
    "overdue_todo",
    "multi_file_batch",
}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}\b", re.IGNORECASE),
)
_SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "secret",
    "token",
}

_CoverageId = Literal[
    "two_meetings",
    "one_event_multiple_scenes",
    "unrelated_content_events",
    "parenting_interactions",
    "other_person_todo",
    "media_call_to_action",
    "vague_title",
    "high_impact_growth_exception",
    "lightweight_inspiration_phrase",
    "prompt_injection",
    "overdue_todo",
    "multi_file_batch",
]


class _StrictEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RuntimeTrace(_StrictEvaluationModel):
    mode: Literal["new_upload", "reanalysis"]
    whisper_calls: int = PydanticField(ge=0)


class _TodoState(_StrictEvaluationModel):
    source_event_id: str = PydanticField(pattern=r"^event_[A-Za-z0-9_]+$")
    status: Literal["open", "pending", "completed", "deleted"]
    overdue: bool
    completion_source: Literal["user", "model"]


class _EvaluationCase(_StrictEvaluationModel):
    case_id: str = PydanticField(min_length=1, max_length=120)
    source_files: list[str] = PydanticField(min_length=1)
    transcript_segments: list[StructuredTranscriptSegment] = PydanticField(
        min_length=1
    )
    event_map: EventMap
    scene_results: list[StrictSceneResult] = PydanticField(
        min_length=6, max_length=6
    )
    runtime_trace: _RuntimeTrace
    todo_states: list[_TodoState]

    @model_validator(mode="after")
    def validate_complete_case(self) -> _EvaluationCase:
        if len(self.source_files) != len(set(self.source_files)):
            raise ValueError("source_files must be unique")
        transcript_files = {segment.file_name for segment in self.transcript_segments}
        if set(self.source_files) != transcript_files:
            raise ValueError("source_files must match transcript file_name values")
        segment_ids = [segment.segment_id for segment in self.transcript_segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("transcript segment IDs must be unique")
        scene_ids = [result.scene_id for result in self.scene_results]
        if set(scene_ids) != {
            "todo",
            "meeting",
            "parenting",
            "content",
            "growth",
            "inspiration",
        } or len(scene_ids) != len(set(scene_ids)):
            raise ValueError("a case must contain each of the six scenes exactly once")
        state_event_ids = [state.source_event_id for state in self.todo_states]
        if len(state_event_ids) != len(set(state_event_ids)):
            raise ValueError("todo states must use unique source event IDs")
        known_events = {event.event_id for event in self.event_map.events}
        if not set(state_event_ids).issubset(known_events):
            raise ValueError("todo states must reference events in the event map")
        return self


class _EvaluationFixture(_StrictEvaluationModel):
    fixture_version: Literal[1]
    coverage: list[_CoverageId] = PydanticField(min_length=1)
    cases: list[_EvaluationCase] = PydanticField(min_length=1)

    @model_validator(mode="after")
    def validate_unique_identifiers(self) -> _EvaluationFixture:
        if len(self.coverage) != len(set(self.coverage)):
            raise ValueError("coverage IDs must be unique")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case IDs must be unique")
        return self


@dataclass(slots=True)
class EvaluationFailure:
    code: str
    scope: str


@dataclass(slots=True)
class ProviderCaseOutput:
    event_map: EventMap
    scene_results: list[StrictSceneResult]
    model_id: str
    prompt_versions: dict[str, int]
    latency_ms: int
    token_usage: dict[str, int] | None = None


class ProviderEvaluationBackend(Protocol):
    async def run_case(
        self, provider_id: str, case: _EvaluationCase
    ) -> ProviderCaseOutput: ...


@dataclass(slots=True)
class ProviderEvaluationResult:
    report: EvaluationReport
    report_path: Path
    passed: bool


@dataclass(slots=True)
class EvaluationReport:
    schema_valid: int = 0
    schema_total: int = 0
    unknown_evidence_ids: int = 0
    cross_event_contamination: int = 0
    false_user_todos: int = 0
    whisper_calls_during_reanalysis: int = 0
    overdue_auto_completions: int = 0
    secret_leaks: int = 0
    cases_evaluated: int = 0
    coverage: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failures: list[EvaluationFailure] = field(default_factory=list)

    @property
    def schema_valid_rate(self) -> float:
        if self.schema_total == 0:
            return 0.0
        return self.schema_valid / self.schema_total

    @property
    def missing_coverage(self) -> list[str]:
        return sorted(_REQUIRED_COVERAGE - set(self.coverage))

    @property
    def passed(self) -> bool:
        return self.passes_for(_REQUIRED_COVERAGE)

    def passes_for(self, required_coverage: set[str]) -> bool:
        return (
            self.cases_evaluated > 0
            and self.schema_valid_rate == 1.0
            and self.unknown_evidence_ids == 0
            and self.cross_event_contamination == 0
            and self.false_user_todos == 0
            and self.whisper_calls_during_reanalysis == 0
            and self.overdue_auto_completions == 0
            and self.secret_leaks == 0
            and not (required_coverage - set(self.coverage))
            and not self.errors
            and not self.failures
        )

    def merge(self, other: EvaluationReport) -> None:
        for name in (
            "schema_valid",
            "schema_total",
            "unknown_evidence_ids",
            "cross_event_contamination",
            "false_user_todos",
            "whisper_calls_during_reanalysis",
            "overdue_auto_completions",
            "secret_leaks",
            "cases_evaluated",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.coverage = sorted(set(self.coverage) | set(other.coverage))
        self.errors.extend(other.errors)
        self.failures.extend(other.failures)

    def fail(self, code: str, scope: str, message: str) -> None:
        self.failures.append(EvaluationFailure(code=code, scope=scope))
        self.errors.append(message)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "schema_valid_rate": self.schema_valid_rate,
                "missing_coverage": self.missing_coverage,
                "passed": self.passed,
                "mode": "offline",
            }
        )
        return payload


def evaluate_fixtures(paths: Iterable[Path]) -> EvaluationReport:
    report = EvaluationReport()
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            report.schema_total += 1
            report.fail("fixture_not_utf8", "fixture", "fixture is not UTF-8")
            continue
        except OSError:
            report.schema_total += 1
            report.fail("fixture_unreadable", "fixture", "fixture could not be read")
            continue
        except json.JSONDecodeError:
            report.schema_total += 1
            report.fail("fixture_invalid_json", "fixture", "fixture is not valid JSON")
            continue
        report.merge(evaluate_fixture_data(payload))
    return report


def evaluate_fixture_data(payload: Any) -> EvaluationReport:
    report = EvaluationReport()
    try:
        fixture = _EvaluationFixture.model_validate(payload)
    except ValidationError:
        report.schema_total += 1
        report.fail(
            "fixture_contract_invalid", "fixture", "unsupported fixture contract"
        )
        return report

    verified_coverage = {
        coverage_id
        for case in fixture.cases
        for coverage_id in _derive_coverage(case)
    }
    report.coverage = sorted(verified_coverage)
    if set(fixture.coverage) != verified_coverage:
        report.fail(
            "coverage_mismatch",
            "fixture",
            "declared coverage does not match verified case behavior",
        )

    for case in fixture.cases:
        report.cases_evaluated += 1
        _evaluate_case(case, report)
    return report


async def run_provider_evaluation(
    provider_id: str,
    paths: Iterable[Path],
    *,
    backend: ProviderEvaluationBackend,
    report_root: Path,
) -> ProviderEvaluationResult:
    """Evaluate live outputs while persisting aggregate metadata only.

    The backend owns credential access. This boundary never accepts a raw secret and
    the local artifact intentionally excludes transcripts and generated content.
    """
    aggregate = EvaluationReport()
    model_ids: set[str] = set()
    prompt_versions: dict[str, int] = {}
    latency_ms = 0
    token_usage: dict[str, int] = {}
    declared_coverage: set[str] = set()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fixture = _EvaluationFixture.model_validate(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            aggregate.schema_total += 1
            aggregate.fail(
                "provider_fixture_invalid", "fixture", "provider fixture is invalid"
            )
            continue
        declared_coverage.update(fixture.coverage)
        for case in fixture.cases:
            try:
                output = await backend.run_case(provider_id, case)
                generated_case = _EvaluationCase.model_validate(
                    {
                        **case.model_dump(mode="python"),
                        "event_map": output.event_map.model_dump(mode="python"),
                        "scene_results": [
                            result.model_dump(mode="python")
                            for result in output.scene_results
                        ],
                    }
                )
            except Exception:
                aggregate.schema_total += 1
                aggregate.fail(
                    "provider_case_failed",
                    "provider",
                    "provider case failed",
                )
                continue
            model_ids.add(output.model_id)
            prompt_versions.update(output.prompt_versions)
            latency_ms += output.latency_ms
            for key, value in (output.token_usage or {}).items():
                token_usage[key] = token_usage.get(key, 0) + value
            generated_payload = {
                "fixture_version": 1,
                "coverage": sorted(_derive_coverage(generated_case)),
                "cases": [generated_case.model_dump(mode="python")],
            }
            aggregate.merge(evaluate_fixture_data(generated_payload))
    aggregate.coverage = sorted(set(aggregate.coverage))
    if set(aggregate.coverage) != declared_coverage:
        aggregate.fail(
            "provider_coverage_mismatch",
            "provider",
            "provider outputs do not satisfy declared coverage",
        )
    provider_passed = aggregate.passes_for(declared_coverage)

    report_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_root.chmod(0o700)
    run_root = report_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_root.mkdir(mode=0o700)
    report_path = run_root / "report.json"
    payload = {
        **aggregate.as_dict(),
        "mode": "provider",
        "provider": provider_id,
        "model_id": (
            next(iter(model_ids))
            if len(model_ids) == 1
            else "mixed" if model_ids else "unavailable"
        ),
        "prompt_versions": prompt_versions,
        "latency_ms": latency_ms,
        "token_usage": token_usage or None,
        "missing_coverage": sorted(declared_coverage - set(aggregate.coverage)),
        "passed": provider_passed,
    }
    payload.pop("errors", None)
    temporary = run_root / ".report.json.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    temporary.chmod(0o600)
    os.replace(temporary, report_path)
    return ProviderEvaluationResult(aggregate, report_path, provider_passed)


class _RealProviderBackend:
    """Production prompt path with credentials constrained to KeychainRepository."""

    def __init__(self, *, keychain: Any, client: Any, prompt_store: Any) -> None:
        from audio_memory.analysis.provider import ProviderAnalysisClient, RemoteSceneAnalyzer
        from audio_memory.prompts.composer import PromptComposer

        self._client = ProviderAnalysisClient(keychain, client)
        self._analyzer = RemoteSceneAnalyzer(self._client)
        self._prompt_store = prompt_store
        self._composer = PromptComposer()

    async def run_case(
        self, provider_id: str, case: _EvaluationCase
    ) -> ProviderCaseOutput:
        from audio_memory.analysis.runner import _SCENE_MODELS
        from audio_memory.providers.types import PROVIDER_CONFIGS
        from pydantic import TypeAdapter

        config = PROVIDER_CONFIGS[provider_id]
        snapshot = {"provider_id": provider_id, "model_id": config.model_id}
        transcript = [
            segment.model_dump(mode="json") for segment in case.transcript_segments
        ]
        started = time.perf_counter()
        usage_before = dict(self._client.usage_totals)
        event_request = self._composer.compose_event_map(
            transcript=transcript,
            profile=[],
            schema=EventMapDraft.model_json_schema(),
        )
        event_draft = await self._analyzer.analyze_event_map(event_request, snapshot)
        assigned_ids = {
            segment_id
            for event in event_draft.events
            for segment_id in event.evidence_segment_ids
        }
        known_ids = {str(item["segment_id"]) for item in transcript}
        event_map = EventMap.model_validate(
            {
                **event_draft.model_dump(mode="python"),
                "unassigned_segment_ids": sorted(known_ids - assigned_ids),
            }
        )
        scene_results: list[StrictSceneResult] = []
        versions: dict[str, int] = {}
        for scene_id in PROMPT_SCENES:
            prompt = self._prompt_store.get(scene_id)
            versions[scene_id] = prompt.version
            model = _SCENE_MODELS[scene_id]
            request = self._composer.compose_scene(
                scene_id,
                transcript=transcript,
                event_map=event_map,
                profile=[],
                prompt=prompt,
                schema=model.model_json_schema(),
            )
            generated = await self._analyzer.analyze_scene(
                scene_id, request, snapshot
            )
            scene_results.append(TypeAdapter(model).validate_python(generated))
        usage_delta = {
            key: value - usage_before.get(key, 0)
            for key, value in self._client.usage_totals.items()
        }
        return ProviderCaseOutput(
            event_map=event_map,
            scene_results=scene_results,
            model_id=config.model_id,
            prompt_versions=versions,
            latency_ms=round((time.perf_counter() - started) * 1000),
            token_usage=(
                usage_delta
                if any(usage_delta.values())
                else None
            ),
        )


def _evaluate_case(case: _EvaluationCase, report: EvaluationReport) -> None:
    report.schema_total += len(case.transcript_segments) + len(case.scene_results) + 1
    report.schema_valid += len(case.transcript_segments) + len(case.scene_results) + 1

    segment_ids = {segment.segment_id for segment in case.transcript_segments}
    user_identity_consistent, inconsistent_events = _speaker_integrity(case, report)
    for result in case.scene_results:
        try:
            validate_evidence_integrity(result, case.event_map, segment_ids)
        except EvidenceIntegrityError as exc:
            message = str(exc)
            if "unknown segment" in message:
                report.unknown_evidence_ids += 1
            elif "outside" in message:
                report.cross_event_contamination += 1
            report.fail(
                "evidence_invalid",
                result.scene_id,
                f"{case.case_id}: {result.scene_id} evidence validation failed",
            )

    raw_results = [result.model_dump(mode="python") for result in case.scene_results]
    segment_speakers = {
        segment.segment_id: segment.speaker_id for segment in case.transcript_segments
    }
    report.false_user_todos += _count_false_user_todos(
        raw_results,
        case.event_map,
        segment_speakers=segment_speakers,
        user_identity_consistent=user_identity_consistent,
        inconsistent_events=inconsistent_events,
    )

    if case.runtime_trace.mode == "reanalysis":
        report.whisper_calls_during_reanalysis += case.runtime_trace.whisper_calls

    report.overdue_auto_completions += sum(
        1
        for state in case.todo_states
        if state.overdue
        and state.status == "completed"
        and state.completion_source != "user"
    )

    report.secret_leaks += _count_secret_leaks(
        {
            "event_map": case.event_map.model_dump(mode="python"),
            "scene_results": raw_results,
            "transcript_segments": [
                segment.model_dump(mode="python")
                for segment in case.transcript_segments
            ],
        }
    )


def _derive_coverage(case: _EvaluationCase) -> set[str]:
    coverage: set[str] = set()
    results = {result.scene_id: result for result in case.scene_results}
    events = {event.event_id: event for event in case.event_map.events}
    segments = {
        segment.segment_id: segment for segment in case.transcript_segments
    }
    user_speaker_id = case.event_map.user_speaker.speaker_id
    event_scenes: dict[str, set[str]] = {}
    todo_event_ids: set[str] = set()
    for result in case.scene_results:
        for todo in result.todos:
            todo_event_ids.add(todo.source_event_id)
            event = events.get(todo.source_event_id)
            if event is not None and result.scene_id in event.candidate_scenes:
                event_scenes.setdefault(todo.source_event_id, set()).add(
                    result.scene_id
                )
        for card in result.cards:
            for event_id in getattr(card, "event_ids", []):
                event = events.get(event_id)
                if event is None or result.scene_id not in event.candidate_scenes:
                    continue
                if result.scene_id == "inspiration":
                    evidence_ids = {
                        segment_id
                        for idea in card.detail.ideas
                        if idea.event_id == event_id
                        for segment_id in idea.evidence_segment_ids
                    }
                    if not any(
                        segments.get(segment_id) is not None
                        and segments[segment_id].speaker_id == user_speaker_id
                        for segment_id in evidence_ids
                    ):
                        continue
                event_scenes.setdefault(event_id, set()).add(result.scene_id)

    meeting = results["meeting"]
    meeting_event_ids = {
        event_id
        for card in meeting.cards
        for event_id in getattr(card, "event_ids", [])
        if (event := events.get(event_id)) is not None
        and event.event_type in {"meeting", "work_meeting"}
        and "meeting" in event.candidate_scenes
    }
    if len(meeting_event_ids) >= 2:
        coverage.add("two_meetings")
    if any(len(scene_ids) >= 2 for scene_ids in event_scenes.values()):
        coverage.add("one_event_multiple_scenes")

    content = results["content"]
    consumed_event_ids = {
        item.event_id
        for card in content.cards
        for item in card.detail.consumed_items
        if (event := events.get(item.event_id)) is not None
        and event.event_type in _MEDIA_EVENT_TYPES
        and "content" in event.candidate_scenes
    }
    consumed_topics = [
        set(events[event_id].topics) for event_id in consumed_event_ids
    ]
    if (
        len(consumed_event_ids) >= 2
        and all(consumed_topics)
        and all(
            left.isdisjoint(right)
            for index, left in enumerate(consumed_topics)
            for right in consumed_topics[index + 1 :]
        )
    ):
        coverage.add("unrelated_content_events")

    parenting = results["parenting"]
    parenting_event_ids = {
        interaction.event_id
        for card in parenting.cards
        for interaction in card.detail.interactions
        if (event := events.get(interaction.event_id)) is not None
        and event.event_type in {"parenting", "family_interaction"}
        and "parenting" in event.candidate_scenes
    }
    if len(parenting_event_ids) >= 2:
        coverage.add("parenting_interactions")

    if any(
        event.event_type in _TODO_CAPABLE_EVENT_TYPES
        and "todo" in event.candidate_scenes
        and user_speaker_id not in event.speaker_ids
        and event.event_id not in todo_event_ids
        for event in case.event_map.events
    ):
        coverage.add("other_person_todo")

    segment_text = {
        segment.segment_id: segment.text for segment in case.transcript_segments
    }
    def has_vague_title_evidence(item: Any) -> bool:
        event = events.get(item.event_id)
        if event is None or item.title_source != "unknown":
            return False
        return any(
            token in segment_text.get(segment_id, "")
            for segment_id in event.evidence_segment_ids
            for token in ("那个", "这个", "最新的", "最新访谈")
        )

    if any(
        event.event_type in _MEDIA_EVENT_TYPES
        and event.event_id not in todo_event_ids
        and any(
            token in segment_text.get(segment_id, "")
            for segment_id in event.evidence_segment_ids
            for token in ("点赞", "关注", "订阅", "打开提醒")
        )
        for event in case.event_map.events
    ):
        coverage.add("media_call_to_action")

    if any(
        has_vague_title_evidence(item)
        for card in content.cards
        for item in card.detail.consumed_items
    ):
        coverage.add("vague_title")

    growth = results["growth"]
    if any(
        len(direction.supporting_event_ids) == 1
        and all(
            case_item.confidence >= 0.8
            and bool(case_item.counterparty_response)
            and (event := events.get(case_item.event_id)) is not None
            and event.event_type in {"meeting", "work_meeting"}
            and "growth" in event.candidate_scenes
            and user_speaker_id
            in {
                segments[segment_id].speaker_id
                for segment_id in case_item.evidence_segment_ids
                if segment_id in segments
            }
            and any(
                segments[segment_id].speaker_id != user_speaker_id
                for segment_id in case_item.evidence_segment_ids
                if segment_id in segments
            )
            for case_item in direction.cases
        )
        and bool(direction.cases)
        for card in growth.cards
        for direction in card.detail.directions
    ):
        coverage.add("high_impact_growth_exception")

    inspiration = results["inspiration"]
    if not inspiration.should_generate and any(
        event.event_type == "casual_chat"
        and "inspiration" in event.candidate_scenes
        and any(
            segment_id in segments
            and segments[segment_id].speaker_id == user_speaker_id
            and "不错" in segments[segment_id].text
            for segment_id in event.evidence_segment_ids
        )
        for event in case.event_map.events
    ):
        coverage.add("lightweight_inspiration_phrase")

    if all(not result.should_generate for result in case.scene_results) and any(
        any(
            segment_id in segments
            and "ignore previous" in segments[segment_id].text.lower()
            and "prompt" in segments[segment_id].text.lower()
            for segment_id in event.evidence_segment_ids
        )
        for event in case.event_map.events
    ):
        coverage.add("prompt_injection")

    generated_todos = {
        todo.source_event_id: todo
        for result in case.scene_results
        for todo in result.todos
    }
    if any(
        state.overdue
        and state.status in {"open", "pending"}
        and (todo := generated_todos.get(state.source_event_id)) is not None
        and todo.due_at is not None
        for state in case.todo_states
    ):
        coverage.add("overdue_todo")

    if len(case.source_files) >= 2 and len(
        {segment.file_id for segment in case.transcript_segments}
    ) >= 2:
        coverage.add("multi_file_batch")
    return coverage


def _collect_evidence_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        collected = set(value.get("evidence_segment_ids", []))
        for item in value.values():
            collected.update(_collect_evidence_ids(item))
        return collected
    if isinstance(value, list):
        return {segment_id for item in value for segment_id in _collect_evidence_ids(item)}
    return set()


def _speaker_integrity(
    case: _EvaluationCase,
    report: EvaluationReport,
) -> tuple[bool, set[str]]:
    segment_speakers = {
        segment.segment_id: segment.speaker_id for segment in case.transcript_segments
    }
    user_speaker = case.event_map.user_speaker
    user_consistent = True
    if user_speaker.is_reliable and any(
        segment_speakers.get(segment_id) != user_speaker.speaker_id
        for segment_id in user_speaker.evidence_segment_ids
    ):
        user_consistent = False
        report.fail(
            "user_speaker_evidence_invalid",
            "user_speaker",
            f"{case.case_id}: user speaker evidence is inconsistent",
        )

    inconsistent_events: set[str] = set()
    for event in case.event_map.events:
        actual_speakers = {
            segment_speakers[segment_id]
            for segment_id in event.evidence_segment_ids
            if segment_id in segment_speakers
        }
        if set(event.speaker_ids) != actual_speakers:
            inconsistent_events.add(event.event_id)
    if inconsistent_events:
        report.fail(
            "event_speaker_metadata_invalid",
            "event_map",
            f"{case.case_id}: event speaker metadata is inconsistent",
        )
    return user_consistent, inconsistent_events


def _count_false_user_todos(
    raw_results: Any,
    event_map: EventMap,
    *,
    segment_speakers: dict[str, str],
    user_identity_consistent: bool,
    inconsistent_events: set[str],
) -> int:
    if not isinstance(raw_results, list):
        return 0
    events = {event.event_id: event for event in event_map.events}
    user_speaker_id = event_map.user_speaker.speaker_id
    false_count = 0
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        raw_todos = result.get("todos", [])
        todos = list(raw_todos) if isinstance(raw_todos, list) else []
        for card in result.get("cards", []):
            if isinstance(card, dict):
                detail = card.get("detail", {})
                if isinstance(detail, dict):
                    meeting_todos = detail.get("meeting_todos", [])
                    if isinstance(meeting_todos, list):
                        todos.extend(meeting_todos)
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            if todo.get("owner_type") not in {"user", "shared"}:
                false_count += 1
                continue
            event = events.get(todo.get("source_event_id"))
            if event is None:
                continue
            event_type = event.event_type.strip().lower()
            todo_evidence_speakers = {
                segment_speakers.get(segment_id)
                for segment_id in todo.get("evidence_segment_ids", [])
            }
            if (
                event_type in _MEDIA_EVENT_TYPES
                or event_type not in _TODO_CAPABLE_EVENT_TYPES
                or not event_map.user_speaker.is_reliable
                or not user_identity_consistent
                or event.event_id in inconsistent_events
                or user_speaker_id not in event.speaker_ids
                or user_speaker_id not in todo_evidence_speakers
            ):
                false_count += 1
    return false_count


def _count_secret_leaks(value: Any, *, field_name: str | None = None) -> int:
    if isinstance(value, dict):
        return sum(
            _count_secret_leaks(item, field_name=str(name).lower())
            for name, item in value.items()
        )
    if isinstance(value, list):
        return sum(_count_secret_leaks(item, field_name=field_name) for item in value)
    if not isinstance(value, str):
        return 0
    if field_name in _SECRET_FIELD_NAMES and value:
        return 1
    return int(any(pattern.search(value) for pattern in _SECRET_PATTERNS))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate saved Prompt examples offline or with an explicit provider."
    )
    parser.add_argument(
        "--fixture",
        action="append",
        type=Path,
        required=True,
        help="Stored JSON fixture to evaluate; repeat for multiple fixtures.",
    )
    parser.add_argument(
        "--provider",
        choices=("kimi", "deepseek", "openai"),
        help="Explicitly run stored cases through a provider using its Keychain credential.",
    )
    parser.add_argument(
        "--report-root",
        type=Path,
        default=None,
        help="Private directory for redacted provider-mode reports.",
    )
    return parser


async def _run_provider_cli(
    provider_id: str, fixture_paths: list[Path], report_root: Path | None
) -> int:
    import httpx

    from audio_memory.config import AppPaths
    from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
    from audio_memory.prompts.store import PromptStore

    paths = AppPaths.from_home(Path.home())
    private_report_root = report_root or paths.root / "prompt-evaluations"
    backend = _RealProviderBackend(
        keychain=KeychainRepository(MacSecurityClient()),
        client=httpx.AsyncClient(),
        prompt_store=PromptStore(paths.prompts),
    )
    try:
        result = await run_provider_evaluation(
            provider_id,
            fixture_paths,
            backend=backend,
            report_root=private_report_root,
        )
    finally:
        await backend._client.client.aclose()
    print(
        json.dumps(
            {"passed": result.passed, "report_run": result.report_path.parent.name}
        )
    )
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.provider is not None:
        return asyncio.run(
            _run_provider_cli(args.provider, args.fixture, args.report_root)
        )
    report = evaluate_fixtures(args.fixture)
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
