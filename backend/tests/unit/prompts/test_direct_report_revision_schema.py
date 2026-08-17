from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_memory.prompts.direct_report_revision_schema import (
    TargetedReportRevision,
)


def revision_payload() -> dict[str, object]:
    return {
        "revisions": [
            {
                "section_id": "section_002",
                "title": "Work decision",
                "revised_markdown": "## Work decision\n\nThe corrected analysis.",
                "issues_resolved": ["issue_001"],
                "opportunities_resolved": ["opportunity_001"],
                "evidence_segment_ids": ["seg_0_1"],
                "removes_repetition": False,
                "repetition_reason": None,
            }
        ],
        "unresolved_issue_ids": [],
        "revision_summary": "Corrected the career decision.",
    }


def test_revision_rejects_duplicate_section_ids() -> None:
    payload = revision_payload()
    payload["revisions"].append(dict(payload["revisions"][0]))  # type: ignore[union-attr,index]

    with pytest.raises(ValidationError, match="duplicate"):
        TargetedReportRevision.model_validate(payload)


def test_revision_rejects_issue_both_resolved_and_unresolved() -> None:
    payload = revision_payload()
    payload["unresolved_issue_ids"] = ["issue_001"]

    with pytest.raises(ValidationError, match="both"):
        TargetedReportRevision.model_validate(payload)


def test_revision_requires_markdown_heading_to_match_title() -> None:
    payload = revision_payload()
    payload["revisions"][0]["revised_markdown"] = "## Different title\n\nText."  # type: ignore[index]

    with pytest.raises(ValidationError, match="heading"):
        TargetedReportRevision.model_validate(payload)


def test_one_issue_can_require_revisions_in_multiple_sections() -> None:
    payload = revision_payload()
    payload["revisions"].append({
        "section_id": "section_008",
        "title": "Data boundary",
        "revised_markdown": "## Data boundary\n\nThe same issue is corrected here too.",
        "issues_resolved": ["issue_001"],
        "opportunities_resolved": [],
        "evidence_segment_ids": ["seg_0_1"],
        "removes_repetition": False,
        "repetition_reason": None,
    })

    revision = TargetedReportRevision.model_validate(payload)

    assert len(revision.revisions) == 2


def test_revision_rejects_duplicate_opportunity_resolution() -> None:
    payload = revision_payload()
    payload["revisions"].append({  # type: ignore[union-attr]
        "section_id": "section_008",
        "title": "Knowledge",
        "revised_markdown": "## Knowledge\n\nMore context.",
        "issues_resolved": [],
        "opportunities_resolved": ["opportunity_001"],
        "evidence_segment_ids": ["seg_0_1"],
        "removes_repetition": False,
        "repetition_reason": None,
    })

    with pytest.raises(ValidationError, match="opportunity"):
        TargetedReportRevision.model_validate(payload)
