from __future__ import annotations

from copy import deepcopy
import json
import logging

import pytest

import audio_memory.analysis.single_report_runner as runner_module
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.analysis.single_report_runner import SingleReportRunner
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript
from audio_memory.prompts.composer import PromptComposer


V1 = """# Career choice

## 今天发生了什么，重点改进什么

The career discussion matters.

| 阶段 | 发生的事 | 对应的改进 |
| --- | --- | --- |
| Day | Discussed a startup | Verify the role |

## Work

The reader plans to found a startup. This paragraph contains useful context that must remain.

## Next step

Verify the role and company.

## 数据范围与判断边界

Identity remains uncertain.
"""

V2_SECTION = """## Work

The reader is considering joining a startup. This paragraph contains useful context that must remain."""


def audit_payload(*, mode: str, issue: bool, passed: bool | None = None) -> dict[str, object]:
    total = 69 if issue else 91
    issues = []
    unresolved = []
    if issue:
        issues = [{
            "issue_id": "issue_001",
            "severity": "major",
            "issue_type": "factual_error",
            "section_id": "section_002",
            "problem": "Founding and joining are confused.",
            "importance": "The central career decision is wrong.",
            "required_change": "State that the reader considered joining.",
            "affected_claims": ["plans to found"],
            "evidence_segment_ids": ["seg_0_0"],
            "evidence_excerpts": [{"segment_id": "seg_0_0", "text": "I am considering joining a startup."}],
            "context_excerpts": [],
            "allow_deletion_or_compression": False,
        }]
        unresolved = ["issue_001"]
    return {
        "audit_mode": mode,
        "rubric_version": 1,
        "passed": (not issue) if passed is None else passed,
        "scores": {
            "factual_accuracy": 20 if issue else 28,
            "important_coverage": 20 if issue else 23,
            "analysis_depth": 15 if issue else 18,
            "actionability": 9 if issue else 13,
            "expression_structure": 5 if issue else 9,
            "total": total,
        },
        "deductions": [{"dimension": "factual_accuracy", "points": 10, "reason": "Wrong career decision."}] if issue else [],
        "coverage": {
            "full_transcript_reviewed": mode == "full_v1_audit",
            "reviewed_segment_count": 1 if mode in {"chunk_v1_audit", "full_v1_audit"} else None,
            "total_segment_count": 1 if mode in {"chunk_v1_audit", "full_v1_audit"} else None,
            "unreviewed_ranges": [],
            "summary": "Complete full audit." if mode == "full_v1_audit" else "Bounded final audit.",
        },
        "issues": issues,
        "unresolved_issue_ids": unresolved,
        "summary": "Audit result.",
    }


class PipelineProvider:
    def __init__(
        self,
        *,
        v1_audit: dict[str, object] | Exception,
        revision: dict[str, object] | Exception | None = None,
        final_audit: dict[str, object] | Exception | None = None,
    ) -> None:
        self.v1_audit = v1_audit
        self.revision = revision
        self.final_audit = final_audit
        self.v1_markdown = V1
        self.calls: list[dict[str, object]] = []

    async def generate_markdown(self, provider_id: str, **kwargs: object) -> str:
        self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
        return self.v1_markdown

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
        chunk_value = self.v1_audit
        if not isinstance(chunk_value, Exception):
            chunk_value = deepcopy(chunk_value)
            chunk_value["audit_mode"] = "chunk_v1_audit"
            chunk_value["coverage"] = {
                **chunk_value["coverage"],
                "full_transcript_reviewed": False,
                "reviewed_segment_count": int(kwargs["segment_count"]),
                "total_segment_count": int(kwargs["segment_count"]),
                "unreviewed_ranges": [],
            }
        value = {
            "direct-report-audit-chunk": chunk_value,
            "direct-report-audit-merge": self.v1_audit,
            "direct-report-revision": self.revision,
            "direct-report-audit-final": self.final_audit,
        }[str(kwargs["scene_id"])]
        if isinstance(value, Exception):
            raise value
        assert value is not None
        return json.dumps(value, ensure_ascii=False)


class SplittingProvider(PipelineProvider):
    def __init__(self, total_segments: int) -> None:
        super().__init__(
            v1_audit=audit_payload(mode="full_v1_audit", issue=False)
        )
        self.total_segments = total_segments
        self.chunk_sizes: list[int] = []

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        scene_id = str(kwargs["scene_id"])
        self.calls.append({"scene_id": scene_id, **kwargs})
        if scene_id == "direct-report-audit-chunk":
            segment_count = int(kwargs["segment_count"])
            self.chunk_sizes.append(segment_count)
            if segment_count > 4:
                raise ProviderAnalysisError(
                    "truncated", code="model_output_truncated"
                )
            payload = audit_payload(mode="chunk_v1_audit", issue=False)
            payload["coverage"]["reviewed_segment_count"] = segment_count
            payload["coverage"]["total_segment_count"] = segment_count
            return json.dumps(payload)
        if scene_id == "direct-report-audit-merge":
            payload = audit_payload(mode="full_v1_audit", issue=False)
            payload["coverage"]["reviewed_segment_count"] = self.total_segments
            payload["coverage"]["total_segment_count"] = self.total_segments
            return json.dumps(payload)
        raise AssertionError(scene_id)


class SingleSegmentSplittingProvider(SplittingProvider):
    async def generate(self, provider_id: str, **kwargs: object) -> str:
        if str(kwargs["scene_id"]) == "direct-report-audit-chunk":
            segment_count = int(kwargs["segment_count"])
            self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
            self.chunk_sizes.append(segment_count)
            if segment_count > 1:
                raise ProviderAnalysisError(
                    "truncated", code="model_output_truncated"
                )
            payload = audit_payload(mode="chunk_v1_audit", issue=False)
            payload["coverage"]["reviewed_segment_count"] = segment_count
            payload["coverage"]["total_segment_count"] = segment_count
            return json.dumps(payload)
        return await super().generate(provider_id, **kwargs)


class InterruptedSplittingProvider(SplittingProvider):
    def __init__(self, total_segments: int, *, fail_one_leaf: bool) -> None:
        super().__init__(total_segments)
        self.fail_one_leaf = fail_one_leaf

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        scene_id = str(kwargs["scene_id"])
        if scene_id == "direct-report-audit-chunk":
            segment_count = int(kwargs["segment_count"])
            self.calls.append({"scene_id": scene_id, **kwargs})
            self.chunk_sizes.append(segment_count)
            if segment_count > 4:
                raise ProviderAnalysisError(
                    "truncated", code="model_output_truncated"
                )
            if self.fail_one_leaf:
                self.fail_one_leaf = False
                raise ProviderAnalysisError(
                    "temporary failure", code="provider_unavailable"
                )
            payload = audit_payload(mode="chunk_v1_audit", issue=False)
            payload["coverage"]["reviewed_segment_count"] = segment_count
            payload["coverage"]["total_segment_count"] = segment_count
            return json.dumps(payload)
        return await super().generate(provider_id, **kwargs)


class InvalidEvidenceOnceProvider(PipelineProvider):
    def __init__(self) -> None:
        super().__init__(v1_audit=audit_payload(mode="full_v1_audit", issue=False))
        self.chunk_attempts = 0

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        if str(kwargs["scene_id"]) == "direct-report-audit-chunk":
            self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
            self.chunk_attempts += 1
            payload = audit_payload(
                mode="chunk_v1_audit", issue=self.chunk_attempts == 1
            )
            if self.chunk_attempts == 1:
                payload["issues"][0]["context_excerpts"] = [{
                    "segment_id": "seg_0_0",
                    "text": "This is not present in the transcript.",
                }]
            return json.dumps(payload, ensure_ascii=False)
        return await super().generate(provider_id, **kwargs)


class InvalidChunkUntilSmallProvider(SplittingProvider):
    async def generate(self, provider_id: str, **kwargs: object) -> str:
        if str(kwargs["scene_id"]) == "direct-report-audit-chunk":
            segment_count = int(kwargs["segment_count"])
            self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
            self.chunk_sizes.append(segment_count)
            if segment_count > 4:
                return "The audit is not JSON."
            payload = audit_payload(mode="chunk_v1_audit", issue=False)
            payload["coverage"]["reviewed_segment_count"] = segment_count
            payload["coverage"]["total_segment_count"] = segment_count
            return json.dumps(payload)
        return await super().generate(provider_id, **kwargs)


class InvalidMergeAuditOnceProvider(PipelineProvider):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.merge_attempts = 0

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        if str(kwargs["scene_id"]) == "direct-report-audit-merge":
            self.merge_attempts += 1
            if self.merge_attempts == 1:
                self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
                return "The merged audit follows in JSON."
        return await super().generate(provider_id, **kwargs)


class InvalidMergeAuditOnceSplittingProvider(SplittingProvider):
    def __init__(self, total_segments: int) -> None:
        super().__init__(total_segments)
        self.merge_attempts = 0

    async def generate(self, provider_id: str, **kwargs: object) -> str:
        if str(kwargs["scene_id"]) == "direct-report-audit-merge":
            self.merge_attempts += 1
            if self.merge_attempts == 1:
                self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
                return "The merged audit follows in JSON."
        return await super().generate(provider_id, **kwargs)


class WrongMergeCoverageSplittingProvider(SplittingProvider):
    async def generate(self, provider_id: str, **kwargs: object) -> str:
        if str(kwargs["scene_id"]) == "direct-report-audit-merge":
            self.calls.append({"scene_id": kwargs["scene_id"], **kwargs})
            payload = audit_payload(mode="full_v1_audit", issue=False)
            payload["coverage"]["reviewed_segment_count"] = 2
            payload["coverage"]["total_segment_count"] = 2
            return json.dumps(payload)
        return await super().generate(provider_id, **kwargs)


class GenerationSource:
    async def credential_generation(self, provider_id: str) -> int:
        return 1


class Publisher:
    def __init__(self) -> None:
        self.reports = []

    async def publish(self, version_id, result, profile_candidates, **kwargs):
        self.reports.append(result)
        return {"version_id": version_id}


async def seed(
    database: Database,
    *,
    segment_count: int = 1,
    model_id: str = "deepseek-v4-pro",
) -> None:
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="analyzing"))
        session.add(JobFile(
            id="file-1", job_id="job-1", original_name="day.mp3",
            extension=".mp3", size_bytes=10, sha256="a" * 64,
            duration_ms=10_000, recording_started_at=None,
            recording_time_source="unknown", timezone="Asia/Shanghai",
            position=0, temporary_path="/tmp/day.mp3",
        ))
        for index in range(segment_count):
            session.add(Transcript(
                id=f"transcript-{index}", job_file_id="file-1",
                segment_index=index, speaker_id="speaker_1",
                start_ms=index * 1_000, end_ms=(index + 1) * 1_000,
                text=("I am considering joining a startup. " * 80),
                words_json="[]", risk_classified=True, is_reliable=True,
            ))
        session.add(AnalysisVersion(
            id="version-1", source_job_id="job-1", provider_id="deepseek",
            model_id=model_id, credential_generation=1,
            prompt_snapshot_json=json.dumps({"user-analysis-goal": {"content": "Analyze the day.", "version": 1}}),
            profile_snapshot_json="[]", fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            staged_results_json="{}", status="running", worker_owner_id="worker-1",
        ))
        await session.commit()


def revision_payload() -> dict[str, object]:
    return {
        "revisions": [{
            "section_id": "section_002",
            "title": "Work",
            "revised_markdown": V2_SECTION,
            "issues_resolved": ["issue_001"],
            "evidence_segment_ids": ["seg_0_0"],
            "removes_repetition": False,
            "repetition_reason": None,
        }],
        "unresolved_issue_ids": [],
        "revision_summary": "Corrected the career decision.",
    }


def audit_with_major_and_minor() -> dict[str, object]:
    payload = audit_payload(mode="full_v1_audit", issue=True)
    payload["issues"].append({
        "issue_id": "issue_002", "severity": "minor",
        "issue_type": "actionability", "section_id": "section_003",
        "problem": "The next step lacks a deadline.",
        "importance": "It is harder to execute.",
        "required_change": "Add a tomorrow deadline.",
        "affected_claims": ["Verify the role and company."],
        "evidence_segment_ids": ["report_section_003"],
        "evidence_excerpts": [{
            "segment_id": "report_section_003",
            "text": "Verify the role and company.",
        }],
        "context_excerpts": [], "allow_deletion_or_compression": False,
    })
    payload["unresolved_issue_ids"].append("issue_002")
    return payload


def revision_for_all_issues() -> dict[str, object]:
    payload = revision_payload()
    payload["revisions"].append({
        "section_id": "section_003", "title": "Next step",
        "revised_markdown": "## Next step\n\nVerify the role and company tomorrow.",
        "issues_resolved": ["issue_002"], "evidence_segment_ids": [],
        "removes_repetition": False, "repetition_reason": None,
    })
    return payload


async def run_with(
    tmp_path,
    provider: PipelineProvider,
    *,
    segment_count: int = 1,
    model_id: str = "deepseek-v4-pro",
):
    database = Database(tmp_path / "report.sqlite3")
    await database.create_schema()
    await seed(database, segment_count=segment_count, model_id=model_id)
    publisher = Publisher()
    runner = SingleReportRunner(
        database=database,
        provider=provider,
        publisher=publisher,
        generation_source=GenerationSource(),
    )
    await runner.run("version-1", "worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        staged = json.loads(version.staged_results_json)
    await database.dispose()
    return publisher.reports[0], staged


@pytest.mark.asyncio
async def test_truncated_audit_chunk_is_split_until_each_leaf_succeeds(
    tmp_path,
) -> None:
    provider = SplittingProvider(total_segments=12)

    report, staged = await run_with(tmp_path, provider, segment_count=12)

    assert provider.chunk_sizes[0] > 4
    assert any(size <= 4 for size in provider.chunk_sizes)
    assert staged["direct_report_v1_audit_chunk_results"]
    assert report.quality_metadata.audit_status == "completed"


@pytest.mark.asyncio
async def test_invalid_merged_audit_is_retried_once(tmp_path) -> None:
    provider = InvalidMergeAuditOnceSplittingProvider(total_segments=12)

    report, staged = await run_with(tmp_path, provider, segment_count=12)

    assert provider.merge_attempts == 2
    merge_calls = [
        call for call in provider.calls
        if call["scene_id"] == "direct-report-audit-merge"
    ]
    assert [call.get("repair_attempted", False) for call in merge_calls] == [
        False,
        True,
    ]
    assert staged["direct_report_v1_audit"]["audit_mode"] == "full_v1_audit"
    assert report.quality_metadata.audit_status == "completed"


@pytest.mark.asyncio
async def test_truncated_audit_keeps_splitting_past_normal_depth_until_single_segments(
    tmp_path, caplog,
) -> None:
    provider = SingleSegmentSplittingProvider(total_segments=12)
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    report, staged = await run_with(tmp_path, provider, segment_count=12)

    assert provider.chunk_sizes[-1] == 1
    assert len(staged["direct_report_v1_audit_chunk_results"]) == 12
    assert report.quality_metadata.audit_status == "completed"
    events = [json.loads(record.message) for record in caplog.records if record.message.startswith("{")]
    split_events = [item for item in events if item["event"] == "analysis.report.audit_chunk_split"]
    assert split_events
    assert all(item["reason"] == "model_output_truncated" for item in split_events)
    assert max(item["split_depth"] for item in split_events) >= 3
    assert all("transcript" not in item and "model_output" not in item for item in split_events)


@pytest.mark.asyncio
async def test_resumed_audit_uses_saved_split_tree_without_recalling_parents(
    tmp_path,
) -> None:
    database = Database(tmp_path / "split-resume.sqlite3")
    await database.create_schema()
    await seed(database, segment_count=12)
    first = InterruptedSplittingProvider(12, fail_one_leaf=True)
    runner = SingleReportRunner(
        database=database,
        provider=first,
        publisher=Publisher(),
        generation_source=GenerationSource(),
    )
    with pytest.raises(ProviderAnalysisError):
        await runner.run("version-1", "worker-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        staged = json.loads(version.staged_results_json)
    assert staged["direct_report_v1_audit_chunk_splits"]

    second = InterruptedSplittingProvider(12, fail_one_leaf=False)
    resumed = SingleReportRunner(
        database=database,
        provider=second,
        publisher=Publisher(),
        generation_source=GenerationSource(),
    )
    await resumed.run("version-1", "worker-1")

    assert second.chunk_sizes
    assert all(size <= 4 for size in second.chunk_sizes)
    await database.dispose()


@pytest.mark.asyncio
async def test_invalid_chunk_evidence_retries_only_that_chunk_once(tmp_path) -> None:
    provider = InvalidEvidenceOnceProvider()

    report, staged = await run_with(tmp_path, provider)

    assert provider.chunk_attempts == 2
    assert len(staged["direct_report_v1_audit_chunk_results"]) == 1
    assert report.quality_metadata.audit_status == "completed"


@pytest.mark.asyncio
async def test_repeated_invalid_chunk_is_split_until_smaller_chunks_validate(
    tmp_path,
) -> None:
    provider = InvalidChunkUntilSmallProvider(total_segments=12)

    report, staged = await run_with(tmp_path, provider, segment_count=12)

    assert provider.chunk_sizes.count(12) == 2
    assert any(size <= 4 for size in provider.chunk_sizes)
    assert len(staged["direct_report_v1_audit_chunk_results"]) >= 4
    assert report.quality_metadata.audit_status == "completed"


@pytest.mark.asyncio
async def test_flash_merges_multiple_valid_chunks_without_provider_merge(tmp_path) -> None:
    provider = SplittingProvider(total_segments=12)

    report, staged = await run_with(
        tmp_path,
        provider,
        segment_count=12,
        model_id="deepseek-v4-flash",
    )

    assert len(staged["direct_report_v1_audit_chunks"]) > 1
    assert "direct-report-audit-merge" not in {
        call["scene_id"] for call in provider.calls
    }
    assert report.quality_metadata.audit_status == "completed"


@pytest.mark.asyncio
async def test_clean_single_chunk_v1_audit_publishes_without_merge(tmp_path) -> None:
    provider = PipelineProvider(
        v1_audit=audit_payload(mode="full_v1_audit", issue=False)
    )

    report, staged = await run_with(tmp_path, provider)

    assert [call["scene_id"] for call in provider.calls] == [
        "direct-report", "direct-report-audit-chunk"
    ]
    assert report.report_markdown.startswith(V1.strip())
    assert "本次报告：" in report.report_markdown
    assert "首次全量审核：91 分" in report.report_markdown
    assert report.quality_metadata.audit_status == "completed"
    assert report.quality_metadata.quality_score == 91
    assert staged["direct_report_publication_metadata"]["report_version"] == "v1"


@pytest.mark.asyncio
async def test_report_runner_persists_and_logs_each_user_visible_phase(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "report-phases.sqlite3")
    await database.create_schema()
    await seed(database)
    runner = SingleReportRunner(
        database=database,
        provider=PipelineProvider(
            v1_audit=audit_payload(mode="full_v1_audit", issue=True),
            revision=revision_payload(),
            final_audit=audit_payload(
                mode="revision_final_audit", issue=False
            ),
        ),
        publisher=Publisher(),
        generation_source=GenerationSource(),
    )
    phases: list[str] = []
    logged_phases: list[str] = []
    monkeypatch.setattr(
        runner_module,
        "emit_analysis_event",
        lambda _logger, event, **fields: (
            logged_phases.append(fields["status"])
            if event == "analysis.report.phase_changed"
            else None
        ),
    )
    save_phase = runner._set_report_phase

    async def capture_phase(version_id, phase, worker_owner_id):
        await save_phase(version_id, phase, worker_owner_id)
        async with database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            phases.append(json.loads(version.pipeline_checkpoints_json)["report_phase"])

    runner._set_report_phase = capture_phase
    await runner.run("version-1", "worker-1")

    assert phases == ["generating", "auditing", "revising", "auditing", "publishing"]
    assert logged_phases == phases
    await database.dispose()


@pytest.mark.asyncio
async def test_merged_audit_rejects_coverage_that_does_not_match_transcript(
    tmp_path,
) -> None:
    provider = WrongMergeCoverageSplittingProvider(total_segments=12)

    with pytest.raises(ProviderAnalysisError) as captured:
        await run_with(tmp_path, provider, segment_count=12)

    assert captured.value.code == "report_audit_pending"
    assert captured.value.retriable is True


@pytest.mark.asyncio
async def test_material_issue_runs_bounded_revision_and_final_audit(tmp_path) -> None:
    provider = PipelineProvider(
        v1_audit=audit_payload(mode="full_v1_audit", issue=True),
        revision=revision_payload(),
        final_audit=audit_payload(mode="revision_final_audit", issue=False),
    )

    report, staged = await run_with(tmp_path, provider)

    assert [call["scene_id"] for call in provider.calls] == [
        "direct-report", "direct-report-audit-chunk",
        "direct-report-revision", "direct-report-audit-final",
    ]
    assert "considering joining a startup" in report.report_markdown
    assert "untrusted_transcript" not in str(provider.calls[2]["user"])
    assert "untrusted_transcript" not in str(provider.calls[3]["user"])
    assert report.quality_metadata.report_version == "v2"
    assert report.quality_metadata.quality_score_scope == "v2_final_audit"
    assert staged["direct_report_v2_final_audit"]["scores"]["total"] == 91
    assert "定向修改增益：69 → 91（+22）；" in report.report_markdown
    assert "定向终审，非全量回归" not in report.report_markdown


@pytest.mark.asyncio
async def test_revision_authorizes_every_audit_issue_section_regardless_of_severity(tmp_path) -> None:
    provider = PipelineProvider(
        v1_audit=audit_with_major_and_minor(),
        revision=revision_for_all_issues(),
        final_audit=audit_payload(mode="revision_final_audit", issue=False),
    )

    report, _ = await run_with(tmp_path, provider)

    assert report.quality_metadata.report_version == "v2"
    assert "company tomorrow" in report.report_markdown
    revision_request = str(provider.calls[2]["user"])
    assert '"section_id":"section_002"' in revision_request
    assert '"section_id":"section_003"' in revision_request


@pytest.mark.asyncio
async def test_minor_only_audit_issues_still_run_revision(tmp_path) -> None:
    audit = audit_payload(mode="full_v1_audit", issue=False)
    audit["issues"] = [audit_with_major_and_minor()["issues"][1]]
    audit["unresolved_issue_ids"] = ["issue_002"]
    audit["passed"] = True
    revision = revision_for_all_issues()
    revision["revisions"] = [revision["revisions"][1]]
    provider = PipelineProvider(
        v1_audit=audit,
        revision=revision,
        final_audit=audit_payload(mode="revision_final_audit", issue=False),
    )

    report, _ = await run_with(tmp_path, provider)

    assert [call["scene_id"] for call in provider.calls] == [
        "direct-report", "direct-report-audit-chunk",
        "direct-report-revision", "direct-report-audit-final",
    ]
    assert report.quality_metadata.report_version == "v2"


@pytest.mark.asyncio
async def test_v1_audit_transport_failure_remains_retryable(tmp_path) -> None:
    provider = PipelineProvider(v1_audit=RuntimeError("audit timeout"))

    with pytest.raises(ProviderAnalysisError) as failure:
        await run_with(tmp_path, provider)

    assert failure.value.code == "report_audit_pending"
    assert "audit timeout" in str(failure.value)
    assert [call["scene_id"] for call in provider.calls] == [
        "direct-report", "direct-report-audit-chunk"
    ]


@pytest.mark.asyncio
async def test_flash_audit_failure_publishes_complete_v1_as_unaudited(tmp_path) -> None:
    provider = PipelineProvider(v1_audit=RuntimeError("audit timeout"))

    report, staged = await run_with(
        tmp_path,
        provider,
        model_id="deepseek-v4-flash",
    )

    assert report.report_markdown.startswith(V1.strip())
    assert report.quality_metadata.report_version == "v1"
    assert report.quality_metadata.audit_status == "completed_unaudited"
    assert report.quality_metadata.quality_score is None
    assert staged["direct_report_v1_audit_error"]["type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_flash_audit_failure_does_not_publish_incomplete_v1(tmp_path) -> None:
    provider = PipelineProvider(v1_audit=RuntimeError("audit timeout"))
    provider.v1_markdown = "# Incomplete report\n\nOnly a fragment."

    with pytest.raises(ProviderAnalysisError) as failure:
        await run_with(tmp_path, provider, model_id="deepseek-v4-flash")

    assert failure.value.code == "report_incomplete"


@pytest.mark.asyncio
async def test_revision_failure_publishes_scored_v1(tmp_path) -> None:
    provider = PipelineProvider(
        v1_audit=audit_payload(mode="full_v1_audit", issue=True),
        revision=RuntimeError("revision timeout"),
    )

    report, staged = await run_with(tmp_path, provider)

    assert report.report_markdown.startswith(V1.strip())
    assert "首次全量审核：69 分" in report.report_markdown
    assert report.quality_metadata.audit_status == "completed_v1_revision_failed"
    assert report.quality_metadata.quality_score == 69
    assert report.quality_metadata.quality_score_scope == "v1_full_audit"
    assert staged["direct_report_v2_revision_error"]["type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_final_audit_failure_publishes_v2_with_v1_score_scope(tmp_path) -> None:
    provider = PipelineProvider(
        v1_audit=audit_payload(mode="full_v1_audit", issue=True),
        revision=revision_payload(),
        final_audit=RuntimeError("final audit timeout"),
    )

    report, staged = await run_with(tmp_path, provider)

    assert "considering joining a startup" in report.report_markdown
    assert report.quality_metadata.audit_status == "completed_v2_final_audit_degraded"
    assert report.quality_metadata.quality_score == 69
    assert report.quality_metadata.quality_score_scope == "v1_pre_revision"
    assert staged["direct_report_v2_final_audit_error"]["type"] == "RuntimeError"
