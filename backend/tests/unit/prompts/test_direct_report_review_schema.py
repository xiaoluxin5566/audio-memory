import pytest
from pydantic import ValidationError

from audio_memory.prompts.direct_report_review_schema import DirectReportReview


def valid_review():
    return {
        "review_passed": False,
        "issues": [{
            "issue_id": "issue_001", "severity": "major", "category": "missing_todo",
            "section_id": "section_002", "description": "遗漏补发简历待办。",
            "evidence_segment_ids": ["seg_0_1"],
        }],
        "revised_sections": [{
            "section_id": "section_002", "title": "工作与求职",
            "revised_markdown": "## 工作与求职\n\n补充了明确待办和完整分析。",
            "change_kind": "factual",
            "issues_resolved": ["issue_001"],
            "evidence_segment_ids": ["seg_0_1"],
            "preserved_facts": ["参加教育公司面试"],
            "preserved_quotes": [], "preserved_todos": ["补发简历"],
            "removes_repetition": False, "repetition_reason": None,
        }],
    }


def test_review_schema_accepts_evidence_backed_local_revision():
    review = DirectReportReview.model_validate(valid_review())
    assert review.revised_sections[0].section_id == "section_002"


def test_review_schema_accepts_a_natural_revised_page_title():
    value = valid_review()
    value["revised_title"] = "下一份工作要选对，和孩子沟通要慢一点"

    review = DirectReportReview.model_validate(value)

    assert review.revised_title == "下一份工作要选对，和孩子沟通要慢一点"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["revised_sections"].append(dict(value["revised_sections"][0])),
        lambda value: value["revised_sections"][0].update(evidence_segment_ids=[]),
        lambda value: value["revised_sections"][0].update(issues_resolved=[]),
        lambda value: value["revised_sections"][0].update(revised_markdown="## 工作与求职\n\n<script>bad()</script>"),
    ],
)
def test_review_schema_rejects_unsafe_or_unverifiable_revision(mutate):
    value = valid_review()
    mutate(value)
    with pytest.raises(ValidationError):
        DirectReportReview.model_validate(value)


def test_review_cannot_claim_pass_with_critical_issue_or_revisions():
    value = valid_review()
    value["review_passed"] = True
    with pytest.raises(ValidationError):
        DirectReportReview.model_validate(value)


def test_review_cannot_claim_pass_while_revising_the_page_title():
    value = {"review_passed": True, "issues": [], "revised_sections": [], "revised_title": "改后的标题"}
    with pytest.raises(ValidationError):
        DirectReportReview.model_validate(value)
