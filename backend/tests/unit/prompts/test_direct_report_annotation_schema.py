import pytest
from pydantic import ValidationError

from audio_memory.prompts.direct_report_annotation_schema import DirectReportAnnotations


def test_annotation_schema_contains_only_block_reference_and_type():
    value = DirectReportAnnotations.model_validate({
        "annotations": [{"block_id": "block_001", "type": "paragraph"}]
    })
    assert value.annotations[0].block_id == "block_001"


def test_annotation_schema_rejects_text_or_unknown_type():
    with pytest.raises(ValidationError):
        DirectReportAnnotations.model_validate({
            "annotations": [{"block_id": "block_001", "type": "paragraph", "text": "不能返回正文"}]
        })
    with pytest.raises(ValidationError):
        DirectReportAnnotations.model_validate({
            "annotations": [{"block_id": "block_001", "type": "card"}]
        })
