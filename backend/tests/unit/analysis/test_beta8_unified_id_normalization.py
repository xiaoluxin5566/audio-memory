from audio_memory.analysis.beta8_runner import _normalize_unified_v1_segment_ids
from audio_memory.prompts.beta8_scene_schema import (
    BETA8_SCENE_IDS,
    Beta8UnifiedV1Result,
)


def test_unified_v1_corrects_unique_file_prefix_typo_in_every_evidence_field() -> None:
    scenes = [
        {
            "scene_id": scene_id,
            "cards": [],
            "todo_candidates": [],
            "skip_reason": "no card",
        }
        for scene_id in BETA8_SCENE_IDS
    ]
    scenes[0] = {
        "scene_id": "work_communication",
        "cards": [{
            "markdown": "# Project update\n\nThe team confirmed the plan.\n\n## Next\nProceed.",
            "draft_card_key": "work-1",
            "card_basis": {
                "type": "work_communication",
                "source_unit_ids": ["session-1"],
                "communication_kind": "other",
            },
            "source_segment_ids": ["seg_3_2879"],
            "search_candidates": [{
                "question": "verify",
                "purpose": "fact check",
                "related_segment_ids": ["seg_3_2879"],
            }],
        }],
        "todo_candidates": [{
            "text": "proceed",
            "owner_type": "user",
            "assignee_text": None,
            "due_at": None,
            "due_text": None,
            "evidence_segment_ids": ["seg_3_2879"],
        }],
        "skip_reason": None,
    }
    result = Beta8UnifiedV1Result.model_validate({
        "input_complete": True,
        "input_error": None,
        "scene_results": scenes,
        "omitted_units": [],
    })

    corrected = _normalize_unified_v1_segment_ids(
        result, known_segment_ids={"seg_2_2879"}
    )

    card = corrected.scene_results[0].cards[0]
    assert card.source_segment_ids == ["seg_2_2879"]
    assert card.search_candidates[0].related_segment_ids == ["seg_2_2879"]
    assert corrected.scene_results[0].todo_candidates[0].evidence_segment_ids == [
        "seg_2_2879"
    ]
