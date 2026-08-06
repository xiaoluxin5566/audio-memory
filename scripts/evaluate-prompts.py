#!/usr/bin/env python3
"""Deterministic, offline release gate for stored Prompt evaluation examples."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field as PydanticField,
    ValidationError,
    model_validator,
)

from audio_memory.prompts.event_schema import EventMap, StructuredTranscriptSegment
from audio_memory.prompts.evidence import (
    EvidenceIntegrityError,
    validate_evidence_integrity,
)
from audio_memory.prompts.schemas import StrictSceneResult


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
        return (
            self.cases_evaluated > 0
            and self.schema_valid_rate == 1.0
            and self.unknown_evidence_ids == 0
            and self.cross_event_contamination == 0
            and self.false_user_todos == 0
            and self.whisper_calls_during_reanalysis == 0
            and self.overdue_auto_completions == 0
            and self.secret_leaks == 0
            and not self.missing_coverage
            and not self.errors
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
        except (OSError, json.JSONDecodeError):
            report.schema_total += 1
            report.errors.append("fixture could not be loaded")
            continue
        report.merge(evaluate_fixture_data(payload))
    return report


def evaluate_fixture_data(payload: Any) -> EvaluationReport:
    report = EvaluationReport()
    try:
        fixture = _EvaluationFixture.model_validate(payload)
    except ValidationError:
        report.schema_total += 1
        report.errors.append("unsupported fixture contract")
        return report

    verified_coverage = {
        coverage_id
        for case in fixture.cases
        for coverage_id in _derive_coverage(case)
    }
    report.coverage = sorted(verified_coverage)
    if set(fixture.coverage) != verified_coverage:
        report.errors.append("declared coverage does not match verified case behavior")

    for case in fixture.cases:
        report.cases_evaluated += 1
        _evaluate_case(case, report)
    return report


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
            report.errors.append(
                f"{case.case_id}: {result.scene_id} evidence validation failed"
            )

    raw_results = [result.model_dump(mode="python") for result in case.scene_results]
    report.false_user_todos += _count_false_user_todos(
        raw_results,
        case.event_map,
        user_identity_consistent=user_identity_consistent,
        inconsistent_events=inconsistent_events,
    )

    if case.runtime_trace.mode == "reanalysis":
        report.whisper_calls_during_reanalysis += case.runtime_trace.whisper_calls

    report.overdue_auto_completions += sum(
        1
        for state in case.todo_states
        if state.overdue and state.status not in {"open", "pending"}
    )

    report.secret_leaks += _count_secret_leaks(
        {
            "event_map": case.event_map.model_dump(mode="python"),
            "scene_results": raw_results,
        }
    )


def _derive_coverage(case: _EvaluationCase) -> set[str]:
    coverage: set[str] = set()
    results = {result.scene_id: result for result in case.scene_results}
    event_scenes: dict[str, set[str]] = {}
    todo_event_ids: set[str] = set()
    for result in case.scene_results:
        for todo in result.todos:
            todo_event_ids.add(todo.source_event_id)
            event_scenes.setdefault(todo.source_event_id, set()).add(result.scene_id)
        for card in result.cards:
            for event_id in getattr(card, "event_ids", []):
                event_scenes.setdefault(event_id, set()).add(result.scene_id)

    meeting = results["meeting"]
    if len(meeting.cards) >= 2:
        coverage.add("two_meetings")
    if any(len(scene_ids) >= 2 for scene_ids in event_scenes.values()):
        coverage.add("one_event_multiple_scenes")

    content = results["content"]
    if any(
        len({item.event_id for item in card.detail.consumed_items}) >= 2
        for card in content.cards
    ):
        coverage.add("unrelated_content_events")

    parenting = results["parenting"]
    if any(len(card.detail.interactions) >= 2 for card in parenting.cards):
        coverage.add("parenting_interactions")

    user_speaker_id = case.event_map.user_speaker.speaker_id
    if any(
        "todo" in event.candidate_scenes
        and user_speaker_id not in event.speaker_ids
        and event.event_id not in todo_event_ids
        for event in case.event_map.events
    ):
        coverage.add("other_person_todo")

    segment_text = {
        segment.segment_id: segment.text for segment in case.transcript_segments
    }
    events = {event.event_id: event for event in case.event_map.events}

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
            for case_item in direction.cases
        )
        for card in growth.cards
        for direction in card.detail.directions
    ):
        coverage.add("high_impact_growth_exception")

    inspiration = results["inspiration"]
    if not inspiration.should_generate and any(
        "不错" in segment.text for segment in case.transcript_segments
    ):
        coverage.add("lightweight_inspiration_phrase")

    if all(not result.should_generate for result in case.scene_results) and any(
        "ignore previous" in segment.text.lower()
        and "prompt" in segment.text.lower()
        for segment in case.transcript_segments
    ):
        coverage.add("prompt_injection")

    if any(
        state.overdue and state.status in {"open", "pending"}
        for state in case.todo_states
    ):
        coverage.add("overdue_todo")

    if len(case.source_files) >= 2 and len(
        {segment.file_id for segment in case.transcript_segments}
    ) >= 2:
        coverage.add("multi_file_batch")
    return coverage


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
        report.errors.append(f"{case.case_id}: user speaker evidence is inconsistent")

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
        report.errors.append(f"{case.case_id}: event speaker metadata is inconsistent")
    return user_consistent, inconsistent_events


def _count_false_user_todos(
    raw_results: Any,
    event_map: EventMap,
    *,
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
            if (
                event_type in _MEDIA_EVENT_TYPES
                or event_type not in _TODO_CAPABLE_EVENT_TYPES
                or not event_map.user_speaker.is_reliable
                or not user_identity_consistent
                or event.event_id in inconsistent_events
                or user_speaker_id not in event.speaker_ids
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
        description="Evaluate saved Prompt examples without network or provider access."
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
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.provider is not None:
        parser.error("offline-only evaluator does not execute provider requests")
    report = evaluate_fixtures(args.fixture)
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
