"""Explicit bounds must never silently absorb intervening content."""
import pytest

from audio_memory.analysis.beta8_event_index import (
    EventIndexCoverageError, normalize_event_index_draft,
)
from audio_memory.prompts.beta8_event_index_schema import Beta8EventIndexDraft


def draft_payload():
    return {
        "input_complete": True,
        "primary_sessions": [{
            "session_id": "conversation", "activity_kind": "conversation",
            "communication_purpose": "non_work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "input_start",
            "start_boundary_evidence_segment_ids": ["s1"],
            "end_boundary": "activity_ended",
            "end_boundary_evidence_segment_ids": ["s2"],
            "subject": "conversation", "description": "A short conversation.",
        }],
        "file_timelines": [{"source_file": "a", "blocks": [
            {"start_segment_id": "s1", "end_segment_id": "s2",
             "disposition": "session", "session_id": "conversation"},
            {"start_segment_id": "s3", "end_segment_id": "s4",
             "disposition": "excluded", "excluded_reason": "noise"},
        ]}],
    }


def transcript():
    return [{"file_id": "a", "segment_id": f"s{i}", "text": "text"}
            for i in range(1, 5)]


def test_explicit_end_preserves_noise_instead_of_absorbing_it():
    result = normalize_event_index_draft(
        Beta8EventIndexDraft.model_validate(draft_payload()), transcript())
    assert result.primary_sessions[0].ranges[0].end_segment_id == "s2"
    assert result.coverage_ranges[1].disposition == "excluded"


@pytest.mark.parametrize("end", ["s1", "s3", "s99"])
def test_explicit_gap_overlap_or_unknown_end_cannot_be_silently_repaired(end):
    payload = draft_payload()
    payload["file_timelines"][0]["blocks"][0]["end_segment_id"] = end
    draft = Beta8EventIndexDraft.model_validate(payload)
    with pytest.raises(EventIndexCoverageError):
        normalize_event_index_draft(draft, transcript())


def test_explicit_final_end_cannot_silently_swallow_unaccounted_tail():
    payload = draft_payload()
    payload["file_timelines"][0]["blocks"][1]["end_segment_id"] = "s3"
    draft = Beta8EventIndexDraft.model_validate(payload)
    with pytest.raises(EventIndexCoverageError):
        normalize_event_index_draft(draft, transcript())


def test_explicit_ranges_do_not_turn_eight_segment_distance_into_semantic_truth():
    payload = draft_payload()
    payload["file_timelines"][0]["blocks"] = [
        {"start_segment_id": "s1", "end_segment_id": "s20",
         "disposition": "session", "session_id": "conversation"}]
    rows = [{"file_id": "a", "segment_id": f"s{i}", "text": "text"}
            for i in range(1, 21)]
    result = normalize_event_index_draft(
        Beta8EventIndexDraft.model_validate(payload), rows)
    assert result.primary_sessions[0].ranges[0].end_segment_id == "s20"
