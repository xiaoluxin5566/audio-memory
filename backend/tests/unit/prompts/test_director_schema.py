from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_memory.prompts.director_schema import DirectorResult


def selection_payload() -> dict[str, object]:
    return {
        "selection_id": "selection_001",
        "cluster_ids": ["cluster_1234567890abcdefabcd"],
        "source_event_ids": ["event_001"],
        "candidate_scenes": ["meeting", "todo"],
        "title": "组织调整期间的业务推进沟通",
        "selection_reason": "讨论了组织安排和待确认的推进事项。",
        "value_signals": [
            "role_or_org_change",
            "follow_up_needed",
        ],
        "priority": "high",
        "context_before_clusters": 0,
        "context_after_clusters": 1,
    }


def test_director_result_accepts_one_selection_for_multiple_scenes() -> None:
    result = DirectorResult.model_validate({"selections": [selection_payload()]})

    assert result.selections[0].candidate_scenes == ["meeting", "todo"]
    assert result.selections[0].context_after_clusters == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_scenes", ["invented"]),
        ("value_signals", ["invented"]),
        ("priority", "urgent"),
        ("context_before_clusters", 2),
        ("cluster_ids", []),
        ("cluster_ids", ["cluster_1234567890abcdefabcd"] * 2),
        ("source_event_ids", ["event_001", "event_001"]),
        ("candidate_scenes", ["meeting", "meeting"]),
        ("value_signals", ["new_idea", "new_idea"]),
    ],
)
def test_director_result_rejects_invalid_or_duplicate_values(
    field: str, value: object
) -> None:
    payload = selection_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        DirectorResult.model_validate({"selections": [payload]})


def test_director_result_rejects_duplicate_selection_ids_and_unknown_fields() -> None:
    first = selection_payload()
    second = {**selection_payload(), "title": "另一段场景"}

    with pytest.raises(ValidationError, match="selection_id"):
        DirectorResult.model_validate({"selections": [first, second]})
    with pytest.raises(ValidationError):
        DirectorResult.model_validate(
            {"selections": [{**first, "final_meeting_summary": "not allowed"}]}
        )
