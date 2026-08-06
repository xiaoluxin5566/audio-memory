from __future__ import annotations

from audio_memory.analysis.todos import reconcile_todos
from audio_memory.models import Todo, TodoCandidate, TodoTombstone


def candidate(
    *,
    candidate_id: str = "candidate-new",
    version_id: str = "version-new",
    event_id: str = "event_planning",
    action: str = "send notes",
    assignee: str | None = "user",
    due_at: str | None = "2026-08-10T09:00:00+08:00",
    fingerprint: str = "source-stable",
    text: str = "明天发出会议纪要",
) -> TodoCandidate:
    return TodoCandidate(
        id=candidate_id,
        analysis_version_id=version_id,
        source_job_id="job-1",
        source_event_id=event_id,
        evidence_segment_ids_json='["seg_0_1"]',
        normalized_action=action,
        normalized_object="meeting notes",
        normalized_assignee=assignee,
        text=text,
        due_at=due_at,
        source_fingerprint=fingerprint,
    )


def existing_todo(**overrides) -> Todo:
    values = {
        "id": "todo-existing",
        "batch_id": "batch-1",
        "analysis_version_id": "version-old",
        "source_job_id": "job-1",
        "source_event_id": "event_planning",
        "evidence_segment_ids_json": '["seg_0_0"]',
        "normalized_action": "send notes",
        "normalized_object": "meeting notes",
        "normalized_assignee": "user",
        "source_fingerprint": "source-stable",
        "text": "发出纪要",
        "due_at": "2026-08-10T08:00:00+08:00",
        "completed": False,
        "user_edited": False,
        "completion_source": "model",
    }
    values.update(overrides)
    return Todo(**values)


def test_stable_source_updates_unedited_model_todo() -> None:
    old = existing_todo()

    reconciled = reconcile_todos(
        "batch-1", [candidate()], [old], []
    )

    assert reconciled == [old]
    assert old.text == "明天发出会议纪要"
    assert old.due_at == "2026-08-10T09:00:00+08:00"
    assert old.analysis_version_id == "version-new"
    assert old.evidence_segment_ids_json == '["seg_0_1"]'


def test_user_edited_text_and_due_date_are_preserved() -> None:
    old = existing_todo(
        user_edited=True,
        text="用户自己的描述",
        due_at="2026-08-10T12:00:00+08:00",
    )

    reconcile_todos("batch-1", [candidate()], [old], [])

    assert old.text == "用户自己的描述"
    assert old.due_at == "2026-08-10T12:00:00+08:00"
    assert old.analysis_version_id == "version-new"


def test_user_edited_deadline_does_not_duplicate_stable_source_on_reanalysis() -> None:
    old = existing_todo(
        user_edited=True,
        text="用户自己的描述",
        due_at="2026-08-15T12:00:00+08:00",
    )

    first = reconcile_todos("batch-1", [candidate()], [old], [])
    second = reconcile_todos(
        "batch-1",
        [candidate(candidate_id="candidate-later", version_id="version-later")],
        first,
        [],
    )

    assert first == [old]
    assert second == [old]
    assert old.text == "用户自己的描述"
    assert old.due_at == "2026-08-15T12:00:00+08:00"
    assert old.analysis_version_id == "version-later"


def test_manually_completed_todo_stays_completed() -> None:
    old = existing_todo(completed=True, completion_source="user")

    reconcile_todos("batch-1", [candidate()], [old], [])

    assert old.completed is True
    assert old.completion_source == "user"


def test_overdue_model_todo_stays_incomplete() -> None:
    old = existing_todo(
        due_at="2020-01-01T00:00:00+00:00",
        completed=False,
    )
    new = candidate(due_at="2020-01-01T01:00:00+00:00")

    reconcile_todos("batch-1", [new], [old], [])

    assert old.completed is False
    assert old.completion_source == "model"


def test_tombstoned_source_is_not_resurrected() -> None:
    reconciled = reconcile_todos(
        "batch-1",
        [candidate()],
        [],
        [TodoTombstone(source_fingerprint="source-stable")],
    )

    assert reconciled == []


def test_ambiguous_candidate_stays_separate_from_existing_todo() -> None:
    old = existing_todo()
    ambiguous = candidate(
        candidate_id="candidate-other-event",
        event_id="event_followup",
        fingerprint="source-other-event",
        text="同样是发纪要，但来源不同",
    )

    reconciled = reconcile_todos("batch-1", [ambiguous], [old], [])

    assert len(reconciled) == 2
    assert reconciled[0] is old
    assert reconciled[1].id != old.id
    assert reconciled[1].source_event_id == "event_followup"


def test_incompatible_due_date_does_not_force_a_merge() -> None:
    old = existing_todo(due_at="2026-08-10T08:00:00+08:00")
    changed_deadline = candidate(due_at="2026-08-12T08:00:00+08:00")

    reconciled = reconcile_todos("batch-1", [changed_deadline], [old], [])

    assert len(reconciled) == 2
    assert old.due_at == "2026-08-10T08:00:00+08:00"
    assert reconciled[1].due_at == "2026-08-12T08:00:00+08:00"
    assert reconciled[1].source_fingerprint != old.source_fingerprint


def test_deadline_disambiguation_is_stable_across_later_reanalysis() -> None:
    old = existing_todo(due_at="2026-08-10T08:00:00+08:00")
    changed_deadline = candidate(
        due_at="2026-08-12T08:00:00+08:00", text="first wording"
    )
    first = reconcile_todos("batch-1", [changed_deadline], [old], [])
    separated_fingerprint = first[1].source_fingerprint

    repeated = candidate(
        due_at="2026-08-12T09:00:00+08:00", text="updated wording"
    )
    second = reconcile_todos("batch-1", [repeated], first, [])

    assert len(second) == 2
    assert second[1].id == first[1].id
    assert second[1].source_fingerprint == separated_fingerprint
    assert second[1].text == "updated wording"


def test_tombstone_for_disambiguated_deadline_blocks_later_candidate() -> None:
    old = existing_todo(due_at="2026-08-10T08:00:00+08:00")
    changed_deadline = candidate(due_at="2026-08-12T08:00:00+08:00")
    separated = reconcile_todos("batch-1", [changed_deadline], [old], [])[1]

    reconciled = reconcile_todos(
        "batch-1",
        [changed_deadline],
        [old],
        [TodoTombstone(source_fingerprint=separated.source_fingerprint)],
    )

    assert reconciled == [old]


def test_different_normalized_objects_are_not_forced_to_merge() -> None:
    old = existing_todo(normalized_object="meeting notes")
    other_object = candidate()
    other_object.normalized_object = "budget report"

    reconciled = reconcile_todos("batch-1", [other_object], [old], [])

    assert len(reconciled) == 2
    assert old.normalized_object == "meeting notes"
    assert reconciled[1].normalized_object == "budget report"
