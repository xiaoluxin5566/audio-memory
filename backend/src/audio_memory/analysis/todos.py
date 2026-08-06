from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from audio_memory.models import Todo, TodoCandidate, TodoTombstone


def reconcile_todos(
    batch_id: str,
    candidates: list[TodoCandidate],
    existing: list[Todo],
    tombstones: list[TodoTombstone],
) -> list[Todo]:
    """Merge exact source matches while retaining every ambiguous old todo."""

    deleted = {item.source_fingerprint for item in tombstones}
    available = list(existing)
    reconciled = list(existing)
    used_fingerprints = {
        todo.source_fingerprint
        for todo in existing
        if todo.source_fingerprint is not None
    }
    for candidate in candidates:
        if (
            candidate.source_fingerprint in deleted
            or _disambiguated_fingerprint(candidate) in deleted
        ):
            continue
        match = next(
            (
                todo
                for todo in available
                if _same_source(todo, candidate)
                and (
                    _protected_exact_fingerprint(todo, candidate)
                    or _compatible_due_at(todo.due_at, candidate.due_at)
                )
            ),
            None,
        )
        if match is None:
            fingerprint = _available_fingerprint(candidate, used_fingerprints)
            reconciled.append(_new_todo(batch_id, candidate, fingerprint))
            used_fingerprints.add(fingerprint)
            continue
        available.remove(match)
        _refresh_from_candidate(match, candidate)
    return reconciled


def _same_source(todo: Todo, candidate: TodoCandidate) -> bool:
    return (
        todo.source_job_id == candidate.source_job_id
        and todo.source_event_id == candidate.source_event_id
        and todo.normalized_action == candidate.normalized_action
        and todo.normalized_object == candidate.normalized_object
        and todo.normalized_assignee == candidate.normalized_assignee
    )


def _protected_exact_fingerprint(todo: Todo, candidate: TodoCandidate) -> bool:
    return (
        todo.user_edited
        and todo.source_fingerprint is not None
        and todo.source_fingerprint == candidate.source_fingerprint
    )


def _compatible_due_at(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return True
    try:
        return datetime.fromisoformat(left).date() == datetime.fromisoformat(right).date()
    except ValueError:
        return left == right


def _refresh_from_candidate(todo: Todo, candidate: TodoCandidate) -> None:
    todo.analysis_version_id = candidate.analysis_version_id
    todo.source_job_id = candidate.source_job_id
    todo.source_event_id = candidate.source_event_id
    todo.evidence_segment_ids_json = candidate.evidence_segment_ids_json
    todo.normalized_action = candidate.normalized_action
    todo.normalized_object = candidate.normalized_object
    todo.normalized_assignee = candidate.normalized_assignee
    if not todo.user_edited:
        todo.text = candidate.text
        todo.due_at = candidate.due_at


def _available_fingerprint(
    candidate: TodoCandidate, used: set[str]
) -> str:
    fingerprint = candidate.source_fingerprint
    if fingerprint not in used:
        return fingerprint
    fingerprint = _disambiguated_fingerprint(candidate)
    counter = 1
    while fingerprint in used:
        fingerprint = sha256(
            f"{candidate.source_fingerprint}\0{_due_key(candidate.due_at)}\0{counter}".encode(
                "utf-8"
            )
        ).hexdigest()
        counter += 1
    return fingerprint


def _disambiguated_fingerprint(candidate: TodoCandidate) -> str:
    return sha256(
        f"{candidate.source_fingerprint}\0{_due_key(candidate.due_at)}".encode(
            "utf-8"
        )
    ).hexdigest()


def _due_key(due_at: str | None) -> str:
    if due_at is None:
        return "no-due-date"
    try:
        return datetime.fromisoformat(due_at).date().isoformat()
    except ValueError:
        return due_at


def _new_todo(
    batch_id: str, candidate: TodoCandidate, source_fingerprint: str
) -> Todo:
    return Todo(
        id=str(
            uuid5(
                NAMESPACE_URL,
                f"audio-memory-todo:{batch_id}:{source_fingerprint}",
            )
        ),
        batch_id=batch_id,
        analysis_version_id=candidate.analysis_version_id,
        source_job_id=candidate.source_job_id,
        source_event_id=candidate.source_event_id,
        evidence_segment_ids_json=candidate.evidence_segment_ids_json,
        normalized_action=candidate.normalized_action,
        normalized_object=candidate.normalized_object,
        normalized_assignee=candidate.normalized_assignee,
        source_fingerprint=source_fingerprint,
        text=candidate.text,
        due_at=candidate.due_at,
        completed=False,
        user_edited=False,
        completion_source="model",
    )
