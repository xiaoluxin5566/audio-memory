from __future__ import annotations

import json
import re

import pytest

from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.analysis.markdown_report import MarkdownReportResult
from audio_memory.analysis.direct_report_document import StructuredReportResult
from audio_memory.analysis.single_report_runner import SingleReportRunner
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript
from audio_memory.prompts.composer import PromptComposer


def valid_markdown(body: str = "工作讨论是今天的主线。") -> str:
    return f"""# 今日录音分析

## 今天发生了什么，重点改进什么

今天的工作讨论值得关注。

| 阶段 | 发生的事 | 对应的改进 |
| --- | --- | --- |
| 全天 | 讨论 AI 硬件 | 明确下一步 |

## 工作判断

{body}

## 下一步

确认负责人和时间。

## 数据范围与判断边界

说话人身份仍需结合上下文判断。"""


def valid_clean_audit(mode: str = "full_v1_audit") -> dict[str, object]:
    return {
        "audit_mode": mode, "rubric_version": 1, "passed": True,
        "scores": {"factual_accuracy": 28, "important_coverage": 23,
                   "analysis_depth": 18, "actionability": 13,
                   "expression_structure": 9, "total": 91},
        "deductions": [],
        "coverage": {"full_transcript_reviewed": mode == "full_v1_audit",
                     "reviewed_segment_count": 1, "total_segment_count": 1,
                     "unreviewed_ranges": [], "summary": "已完整审查。"},
        "issues": [], "unresolved_issue_ids": [], "summary": "通过。",
    }


class FakeProvider:
    def __init__(self, *, structured_response: str | None = None, markdown_responses: list[str | Exception] | None = None, review_response: dict[str, object] | None = None, review_responses: list[dict[str, object]] | None = None, annotation_response: dict[str, object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.structured_response = structured_response
        self.markdown_responses = list(markdown_responses or [valid_markdown()])
        self.review_response = review_response or {"review_passed": True, "issues": [], "revised_sections": []}
        self.review_responses = list(review_responses or [])
        self.annotation_response = annotation_response

    async def generate_markdown(self, provider_id: str, **kwargs: object) -> str:
        self.calls.append({"method": "markdown", "provider_id": provider_id, **kwargs})
        response = self.markdown_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        scene_id = kwargs.get("scene_id")
        if scene_id in {"direct-report-audit-chunk", "direct-report-audit-merge"}:
            self.calls.append({"method": "audit", "provider_id": provider_id, **kwargs})
            mode = "chunk_v1_audit" if scene_id == "direct-report-audit-chunk" else "full_v1_audit"
            return json.dumps(valid_clean_audit(mode), ensure_ascii=False)
        method = "review" if scene_id == "direct-report-review" else "annotations" if scene_id == "direct-report-annotations" else "structured"
        self.calls.append({"method": method, "provider_id": provider_id, **kwargs})
        if method == "review":
            response = self.review_responses.pop(0) if self.review_responses else self.review_response
            return json.dumps(response, ensure_ascii=False)
        if method == "annotations":
            if self.annotation_response is not None:
                return json.dumps(self.annotation_response, ensure_ascii=False)
            block_ids = list(dict.fromkeys(re.findall(r"block_\d+", str(kwargs.get("user", "")))))
            return json.dumps({"annotations": [{"block_id": item, "type": "paragraph"} for item in block_ids]})
        if self.structured_response is not None:
            return self.structured_response
        return json.dumps(valid_structured_document(), ensure_ascii=False)


class FakeGenerationSource:
    async def credential_generation(self, provider_id: str) -> int:
        return 1


class FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.reports: list[MarkdownReportResult | StructuredReportResult] = []

    async def publish(self, version_id, result, profile_candidates, **kwargs):
        self.reports.append(result)
        if self.fail:
            raise RuntimeError("publish failed")
        return {"version_id": version_id}


async def seed(database: Database) -> None:
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="analyzing"))
        session.add(
            JobFile(
                id="file-1", job_id="job-1", original_name="全天.mp3",
                extension=".mp3", size_bytes=10, sha256="a" * 64,
                duration_ms=10_000, recording_started_at=None,
                recording_time_source="unknown", timezone="Asia/Shanghai",
                position=0, temporary_path="/tmp/fixture.mp3",
            )
        )
        session.add(
            Transcript(
                id="transcript-1", job_file_id="file-1", segment_index=0,
                speaker_id="speaker_1", start_ms=0, end_ms=1_000,
                text="我最近主要关注 AI 硬件。", words_json="[]",
                risk_classified=True, is_reliable=True,
            )
        )
        session.add(
            AnalysisVersion(
                id="version-1", source_job_id="job-1", provider_id="deepseek",
                model_id="deepseek-v4-pro", credential_generation=1,
                prompt_snapshot_json=json.dumps({"user-analysis-goal": {"content": "重点看工作。", "version": 1}}),
                profile_snapshot_json=json.dumps([{"dimension": "职业", "value": "AI 硬件从业者"}]),
                fixed_rules_hash=PromptComposer.fixed_rules_hash(),
                staged_results_json="{}", status="running", worker_owner_id="worker-1",
            )
        )
        await session.commit()


def valid_structured_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "AI 硬件讨论",
        "overview": {
            "summary": "你今天重点讨论了 AI 硬件。",
            "rows": [
                {
                    "phase": "全天",
                    "event": "讨论 AI 硬件。",
                    "improvement": "确认下一步。",
                    "evidence_segment_ids": ["seg_0_0"],
                }
            ],
        },
        "sections": [
            {
                "title": "工作判断",
                "blocks": [{"type": "paragraph", "text": "你需要确认下一步。"}],
            }
        ],
        "todos": [],
        "evidence_segment_ids": ["seg_0_0"],
        "external_source_ids": [],
    }


@pytest.mark.asyncio
async def test_single_report_runner_calls_provider_once_with_profile_and_full_transcript(tmp_path) -> None:
    database = Database(tmp_path / "single-report.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider()
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database, provider=provider, publisher=publisher,
        generation_source=FakeGenerationSource(),
    )

    await runner.run("version-1", "worker-1")

    assert len(provider.calls) == 2
    assert provider.calls[0]["method"] == "markdown"
    assert provider.calls[1]["method"] == "audit"
    assert "seg_0_0" in str(provider.calls[1]["user"])
    assert "AI 硬件从业者" in str(provider.calls[0]["user"])
    assert "speaker_1" in str(provider.calls[0]["user"])
    assert "只输出最终 Markdown" in str(provider.calls[0]["system"])
    assert len(publisher.reports) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_single_report_runner_retries_truncated_initial_report_compactly(tmp_path) -> None:
    database = Database(tmp_path / "truncated-initial-report.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider(
        markdown_responses=[
            ProviderAnalysisError(
                "Provider output was truncated", code="model_output_truncated"
            ),
            valid_markdown("压缩重写后仍然保留完整的重要分析。"),
        ]
    )
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=publisher,
        generation_source=FakeGenerationSource(),
    )

    await runner.run("version-1", "worker-1")

    markdown_calls = [call for call in provider.calls if call["method"] == "markdown"]
    assert [call["scene_id"] for call in markdown_calls] == [
        "direct-report",
        "direct-report-compact-retry",
    ]
    assert "speaker_1" in str(markdown_calls[1]["user"])
    assert "12,000" in str(markdown_calls[1]["system"])
    assert len(publisher.reports) == 1
    assert "压缩重写后" in publisher.reports[0].report_markdown
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    staged = json.loads(version.staged_results_json)
    assert "压缩重写后" in staged["direct_report_initial_markdown"]
    assert "分析质量" not in staged["direct_report_initial_markdown"]
    await database.dispose()


@pytest.mark.asyncio
async def test_single_report_runner_does_not_retry_non_truncation_error(tmp_path) -> None:
    database = Database(tmp_path / "initial-report-auth-error.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider(
        markdown_responses=[
            ProviderAnalysisError(
                "Provider credential is unavailable", code="authentication_failed"
            )
        ]
    )
    runner = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=FakePublisher(),
        generation_source=FakeGenerationSource(),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1", "worker-1")

    assert raised.value.code == "authentication_failed"
    assert len(provider.calls) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_single_report_runner_retries_truncated_initial_report_only_once(tmp_path) -> None:
    database = Database(tmp_path / "initial-report-double-truncation.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider(
        markdown_responses=[
            ProviderAnalysisError("first truncation", code="model_output_truncated"),
            ProviderAnalysisError("second truncation", code="model_output_truncated"),
        ]
    )
    runner = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=FakePublisher(),
        generation_source=FakeGenerationSource(),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1", "worker-1")

    assert raised.value.code == "model_output_truncated"
    assert len(provider.calls) == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_single_report_runner_reuses_generated_checkpoint_after_publish_failure(tmp_path) -> None:
    database = Database(tmp_path / "resume-report.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider()
    first = SingleReportRunner(
        database=database, provider=provider, publisher=FakePublisher(fail=True),
        generation_source=FakeGenerationSource(),
    )
    with pytest.raises(RuntimeError, match="publish failed"):
        await first.run("version-1", "worker-1")

    second_publisher = FakePublisher()
    second = SingleReportRunner(
        database=database, provider=provider, publisher=second_publisher,
        generation_source=FakeGenerationSource(),
    )
    await second.run("version-1", "worker-1")

    assert len(provider.calls) == 2
    assert len(second_publisher.reports) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_markdown_mode_supplements_failed_draft_before_publish(tmp_path) -> None:
    database = Database(tmp_path / "supplement-report.sqlite3")
    await database.create_schema()
    await seed(database)
    initial = valid_markdown("初稿工作分析，保留这一句。")
    review = {
        "review_passed": False,
        "revised_title": "下一份工作要选对，和孩子沟通要慢一点",
        "issues": [{
            "issue_id": "issue_001", "severity": "major", "category": "missing_todo",
            "section_id": "section_002", "description": "遗漏补发简历待办。",
            "evidence_segment_ids": ["seg_0_0"],
        }],
        "revised_sections": [{
            "section_id": "section_002", "title": "工作判断",
            "revised_markdown": "## 工作判断\n\n初稿工作分析，保留这一句。补充了明确待办和具体分析。\n\n",
            "change_kind": "factual", "issues_resolved": ["issue_001"],
            "evidence_segment_ids": ["seg_0_0"], "preserved_facts": ["讨论 AI 硬件"],
            "preserved_quotes": [], "preserved_todos": [],
            "removes_repetition": False, "repetition_reason": None,
        }],
    }
    provider = FakeProvider(markdown_responses=[initial], review_response=review)
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database, provider=provider, publisher=publisher,
        generation_source=FakeGenerationSource(),
    )

    await runner.run("version-1", "worker-1")

    assert len(provider.calls) == 2
    assert provider.calls[1]["scene_id"] == "direct-report-audit-chunk"
    assert "初稿工作分析" in str(provider.calls[1]["user"])
    assert "初稿工作分析，保留这一句" in publisher.reports[0].report_markdown
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    staged = json.loads(version.staged_results_json)
    assert staged["direct_report_initial_markdown"] == initial
    assert staged["direct_report_final_markdown"] == publisher.reports[0].report_markdown
    assert staged["direct_report_v1_audit"]["scores"]["total"] == 91
    await database.dispose()


@pytest.mark.asyncio
async def test_markdown_mode_normalizes_overview_heading_before_quality_gate(tmp_path) -> None:
    database = Database(tmp_path / "normalize-overview.sqlite3")
    await database.create_schema()
    await seed(database)
    initial = valid_markdown().replace(
        "## 今天发生了什么，重点改进什么",
        "## 今天发生了什么，重点应该改什么",
    )
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database,
        provider=FakeProvider(markdown_responses=[initial]),
        publisher=publisher,
        generation_source=FakeGenerationSource(),
    )

    await runner.run("version-1", "worker-1")

    assert "## 今天发生了什么，重点改进什么" in publisher.reports[0].report_markdown
    assert "重点应该改什么" not in publisher.reports[0].report_markdown
    await database.dispose()


@pytest.mark.asyncio
async def test_markdown_mode_migrates_overview_heading_in_saved_revision(tmp_path) -> None:
    database = Database(tmp_path / "normalize-saved-revision.sqlite3")
    await database.create_schema()
    await seed(database)
    initial = valid_markdown().replace(
        "## 今天发生了什么，重点改进什么",
        "## 今天发生了什么，重点应该改什么",
    )
    review = {
        "review_passed": False,
        "issues": [{
            "issue_id": "issue_901",
            "severity": "minor",
            "category": "structure",
            "section_id": "section_001",
            "description": "补充总览。",
            "evidence_segment_ids": [],
        }],
        "revised_sections": [{
            "section_id": "section_001",
            "title": "今天发生了什么，重点应该改什么",
            "revised_markdown": initial.split("\n\n## 工作判断", 1)[0].split("\n\n", 1)[1],
            "change_kind": "style",
            "issues_resolved": ["issue_901"],
            "evidence_segment_ids": [],
            "preserved_facts": [],
            "preserved_quotes": [],
            "preserved_todos": [],
            "removes_repetition": False,
            "repetition_reason": None,
        }],
    }
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database,
        provider=FakeProvider(
            markdown_responses=[initial], review_response=review
        ),
        publisher=publisher,
        generation_source=FakeGenerationSource(),
    )

    await runner.run("version-1", "worker-1")

    assert "## 今天发生了什么，重点改进什么" in publisher.reports[0].report_markdown
    await database.dispose()


@pytest.mark.asyncio
async def test_markdown_mode_runs_one_quality_repair_review_before_failing(tmp_path) -> None:
    database = Database(tmp_path / "quality-repair.sqlite3")
    await database.create_schema()
    await seed(database)
    async with database.session() as session:
        transcript = await session.get(Transcript, "transcript-1")
        transcript.text = "我讨论了 AI 硬件。" * 5_000
        await session.commit()
    initial = valid_markdown("初稿分析。")
    repair = {
        "review_passed": False,
        "issues": [{
            "issue_id": "issue_900",
            "severity": "minor",
            "category": "thin_analysis",
            "section_id": "section_002",
            "description": "正文深度不足。",
            "evidence_segment_ids": [],
        }],
        "revised_sections": [{
            "section_id": "section_002",
            "title": "工作判断",
            "revised_markdown": "## 工作判断\n\n" + ("补充具体分析。" * 700) + "\n",
            "change_kind": "analysis",
            "issues_resolved": ["issue_900"],
            "evidence_segment_ids": [],
            "preserved_facts": ["讨论 AI 硬件"],
            "preserved_quotes": [],
            "preserved_todos": [],
            "removes_repetition": False,
            "repetition_reason": None,
        }],
    }
    provider = FakeProvider(
        markdown_responses=[initial],
        review_responses=[
            {"review_passed": True, "issues": [], "revised_sections": []},
            repair,
        ],
    )
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=publisher,
        generation_source=FakeGenerationSource(),
    )

    await runner.run("version-1", "worker-1")

    assert [call["method"] for call in provider.calls] == ["markdown", "audit"]
    assert len(publisher.reports) == 1
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    staged = json.loads(version.staged_results_json)
    assert staged["direct_report_quality"]["quality_score"] == 91
    await database.dispose()


@pytest.mark.asyncio
async def test_quality_repair_failure_persists_diagnostic(tmp_path) -> None:
    database = Database(tmp_path / "quality-repair-diagnostic.sqlite3")
    await database.create_schema()
    await seed(database)
    async with database.session() as session:
        transcript = await session.get(Transcript, "transcript-1")
        transcript.text = "我讨论了 AI 硬件。" * 5_000
        await session.commit()

    class RepairFailingProvider(FakeProvider):
        async def generate(self, provider_id: str, **kwargs: object) -> str:
            if kwargs.get("scene_id") == "direct-report-audit-chunk":
                raise RuntimeError("repair request rejected")
            return await super().generate(provider_id, **kwargs)

    runner = SingleReportRunner(
        database=database,
        provider=RepairFailingProvider(),
        publisher=FakePublisher(),
        generation_source=FakeGenerationSource(),
    )

    with pytest.raises(ProviderAnalysisError) as failure:
        await runner.run("version-1", "worker-1")

    assert failure.value.code == "report_audit_pending"

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    staged = json.loads(version.staged_results_json)
    assert staged["direct_report_v1_audit_error"] == {
        "type": "RuntimeError",
        "message": "repair request rejected",
    }
    await database.dispose()


@pytest.mark.asyncio
async def test_invalid_annotations_fall_back_to_complete_markdown(tmp_path) -> None:
    database = Database(tmp_path / "annotation-fallback.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider(annotation_response={"annotations": []})
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database, provider=provider, publisher=publisher,
        generation_source=FakeGenerationSource(),
    )

    await runner.run("version-1", "worker-1")

    assert len(provider.calls) == 2
    assert publisher.reports[0].report_markdown.startswith(valid_markdown())
    assert "本次报告：" in publisher.reports[0].report_markdown
    assert publisher.reports[0].report_annotations is None
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    staged = json.loads(version.staged_results_json)
    assert "direct_report_annotations" not in staged
    assert "direct_report_annotation_degraded_reason" not in staged
    assert staged["direct_report_quality"]["passed"] is True
    await database.dispose()


@pytest.mark.asyncio
async def test_structured_mode_generates_validated_document_and_checkpoint(tmp_path) -> None:
    database = Database(tmp_path / "structured-report.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider()
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=publisher,
        generation_source=FakeGenerationSource(),
        output_mode="structured",
    )

    await runner.run("version-1", "worker-1")

    assert [call["method"] for call in provider.calls] == ["structured"]
    assert "只输出一个符合 Schema 的 JSON 对象" in str(provider.calls[0]["system"])
    assert isinstance(publisher.reports[0], StructuredReportResult)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    staged = json.loads(version.staged_results_json)
    assert staged["direct_report_output_mode"] == "structured"
    assert staged["direct_report_document"]["schema_version"] == 1
    assert "direct_report_markdown" not in staged
    await database.dispose()


@pytest.mark.asyncio
async def test_structured_mode_reuses_checkpoint_after_publish_failure(tmp_path) -> None:
    database = Database(tmp_path / "structured-resume.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider()
    first = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=FakePublisher(fail=True),
        generation_source=FakeGenerationSource(),
        output_mode="structured",
    )
    with pytest.raises(RuntimeError, match="publish failed"):
        await first.run("version-1", "worker-1")

    second_publisher = FakePublisher()
    second = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=second_publisher,
        generation_source=FakeGenerationSource(),
        output_mode="structured",
    )
    await second.run("version-1", "worker-1")

    assert len(provider.calls) == 1
    assert isinstance(second_publisher.reports[0], StructuredReportResult)
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    ["not json", json.dumps({"schema_version": 1})],
)
async def test_structured_mode_rejects_invalid_response_before_checkpoint_or_publish(
    tmp_path, response: str
) -> None:
    database = Database(tmp_path / "invalid-structured.sqlite3")
    await database.create_schema()
    await seed(database)
    provider = FakeProvider(structured_response=response)
    publisher = FakePublisher()
    runner = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=publisher,
        generation_source=FakeGenerationSource(),
        output_mode="structured",
    )

    with pytest.raises((json.JSONDecodeError, ValueError)):
        await runner.run("version-1", "worker-1")

    assert publisher.reports == []
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert json.loads(version.staged_results_json or "{}") == {}
    await database.dispose()
