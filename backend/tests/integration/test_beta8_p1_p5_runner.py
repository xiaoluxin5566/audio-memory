from __future__ import annotations

from hashlib import sha256
import json

import pytest

from audio_memory.analysis.pipeline_identity import build_pipeline_parameters
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript
from audio_memory.models import Card
from audio_memory.analysis.publisher import VersionPublisher
from sqlalchemy import select


class Generations:
    async def credential_generation(self, provider_id: str) -> int:
        return {"deepseek": 1, "kimi": 2}[provider_id]


class CapturingPublisher:
    def __init__(self):
        self.calls = []

    async def publish_beta8(self, version_id, bundle, *, worker_owner_id, card_assessments):
        self.calls.append((version_id, bundle, worker_owner_id, card_assessments))
        return {"published": True}


def complete_score(card: dict) -> dict:
    return {
        "card_id": card["card_id"], "title": card["title"],
        "card_sha256": sha256(card["markdown"].encode()).hexdigest(),
        "rubric_version": "beta8-quality-2026-09-09-scaled", "status": "scored",
        "dimensions": {"factual_accuracy": 15, "important_coverage": 15,
                       "analysis_depth": 25, "actionability": 30,
                       "expression_structure": 15},
        "raw_total": 100, "capped_reference_total": None, "deductions": [],
        "read_scope": ["activity_1"], "available_scope": "complete_activities",
        "strengths": "完整", "weaknesses": "无", "missing_inputs": [],
    }


class FakePipeline:
    def __init__(self, **kwargs):
        self.output_dir = kwargs["output_dir"]
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def run(self, segments, *, user_corrections, report_period):
        assert user_corrections == []
        assert segments[0]["text"] == "明天去旅行。"
        card = {"status": "written", "card_id": "card-1", "title": "明日行程",
                "markdown": "# 明日行程\n\n明天出发。",
                "evidence_refs": [{"anchor_ids": ["seg_0_0"]}],
                "research_refs": [], "uncertainties": []}
        (self.output_dir / "cards").mkdir(exist_ok=True)
        (self.output_dir / "cards/card-1.input.json").write_text(json.dumps({
            "card_brief": {"scene_id": "parenting_family"}
        }, ensure_ascii=False))
        (self.output_dir / "research-packets.json").write_text("{}")
        return {"status": "scored_v1", "cards": [card],
                "scores": [complete_score(card)], "new_request_count": 4,
                "metrics": {"model_request_count": 4, "report_request_count": 4,
                            "search_model_request_count": 0, "search_tool_call_count": 0,
                            "unresolved_request_count": 0, "input_tokens": 100,
                            "output_tokens": 20, "scoring_model_request_count": 1}}


class FakeModelTransport:
    def __init__(self):
        self.stages = []

    async def complete(self, **kwargs):
        stage = kwargs["stage"]
        data = json.loads(
            kwargs["user"].split(">", 1)[1].rsplit("</task_data>", 1)[0]
        )
        self.stages.append(stage)
        await kwargs["before_request"]({"messages": []})
        await kwargs["after_response"]({
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}
        })
        if stage == "P1":
            value = {"status": "complete", "window_id": data["window_id"],
                "activities": [{"local_key": "a", "start_segment_id": "seg_0_0",
                    "end_segment_id": "seg_0_0", "activity_kind": "conversation",
                    "participation": "live", "purpose": "family", "subject": "明日旅行",
                    "continuation_note": None}],
                "topics": [{"local_key": "t", "understanding": "明天去旅行",
                    "open_questions": [], "evidence_anchor_ids": ["seg_0_0"]}],
                "attention_signals": [{"local_key": "g", "topic_keys": ["t"],
                    "type": "commitment", "description": "确认明天出发",
                    "evidence_anchor_ids": ["seg_0_0"]}], "commitments": [],
                "context_requests": []}
        elif stage == "P2":
            value = {"status": "complete", "cards": [{"draft_key": "trip",
                "scene_id": "parenting_family", "core_question": "明天如何安排？",
                "source_activity_ids": ["activity_001"],
                "topic_assignments": [{"topic_id": "topic_001", "role": "core"}],
                "reader_need": "掌握明日行程", "must_answer": ["明天做什么"],
                "must_cover": [{"description": "明天去旅行",
                    "activity_id": "activity_001", "evidence_anchor_ids": ["seg_0_0"]}],
                "useful_deliverable": "行程提醒",
                "research_decision": {"decision": "no_search", "reason": "原文足够",
                    "task_keys": []}}],
                "topic_dispositions": [{"topic_id": "topic_001", "state": "standalone",
                    "card_keys": ["trip"], "reason": "对明天有用"}],
                "research_tasks": [], "context_requests": [], "budget_gaps": []}
        elif stage == "P4":
            value = {"status": "written", "card_id": data["card_id"],
                "title": "明日行程", "markdown": "# 明日行程\n\n明天去旅行。",
                "evidence_refs": [{"body_locator": "明天去旅行",
                    "anchor_ids": ["seg_0_0"]}], "research_refs": [], "uncertainties": []}
        elif stage == "P5":
            card = data["card"]
            value = {"status": "complete", "card": complete_score(card)}
        else:
            raise AssertionError(stage)
        return json.dumps(value, ensure_ascii=False)


async def seed(db: Database) -> None:
    values, canonical, fingerprint = build_pipeline_parameters(
        pipeline_kind="beta8_p1_p5_v1", provider_id="deepseek",
        model_id="deepseek-v4-pro", credential_generation=1,
        search_provider_id="kimi", search_model_id="kimi-k2.6",
    )
    async with db.session() as session:
        session.add(AnalysisJob(id="job", stage="analyzing"))
        session.add(JobFile(id="file", job_id="job", original_name="day.mp3",
            extension=".mp3", size_bytes=1, sha256="a" * 64, duration_ms=1000,
            recording_started_at="2026-09-10T08:00:00+08:00",
            recording_time_source="metadata", timezone="Asia/Shanghai",
            position=0, temporary_path="/tmp/day.mp3"))
        session.add(Transcript(id="transcript", job_file_id="file", segment_index=0,
            speaker_id="speaker_1", start_ms=0, end_ms=1000, text="明天去旅行。",
            words_json="[]", risk_classified=True, is_reliable=True))
        session.add(AnalysisVersion(id="version", source_job_id="job",
            provider_id="deepseek", model_id="deepseek-v4-pro", credential_generation=1,
            prompt_snapshot_json="{}", profile_snapshot_json="[]",
            fixed_rules_hash=values["fixed_rules_hash"], pipeline_parameters_json=canonical,
            pipeline_parameters_fingerprint=fingerprint, staged_results_json="{}",
            pipeline_metrics_json="{}", status="running", worker_owner_id="owner"))
        await session.commit()


@pytest.mark.asyncio
async def test_formal_runner_executes_and_publishes_without_file_authorization(tmp_path) -> None:
    from audio_memory.analysis.beta8_p1_p5_runner import Beta8P1P5Runner

    db = Database(tmp_path / "runner.sqlite3")
    await db.create_schema(); await seed(db)
    publisher = CapturingPublisher()
    runner = Beta8P1P5Runner(database=db, transport=object(), publisher=publisher,
        generation_source=Generations(), output_root=tmp_path / "artifacts",
        pipeline_factory=FakePipeline)

    result = await runner.run("version", "owner")

    assert result == {"published": True}
    assert publisher.calls[0][1].cards[0].title == "明日行程"
    assert publisher.calls[0][3]["card-1"]["raw_total"] == 100
    assert not (tmp_path / "artifacts/version/authorization.json").exists()
    async with db.session() as session:
        version = await session.get(AnalysisVersion, "version")
    assert json.loads(version.staged_results_json)["beta8_p1_p5_v1"]["status"] == "scored_v1"
    assert json.loads(version.pipeline_metrics_json)["model_call_count"] == 4
    await db.dispose()


@pytest.mark.asyncio
async def test_formal_runner_refuses_incomplete_result(tmp_path) -> None:
    from audio_memory.analysis.beta8_p1_p5_runner import Beta8P1P5Runner
    from audio_memory.analysis.beta8_writing_store import WritingStopped

    class Incomplete(FakePipeline):
        async def run(self, *args, **kwargs):
            return {"status": "draft_ready", "cards": [], "scores": [], "metrics": {}}

    db = Database(tmp_path / "incomplete.sqlite3")
    await db.create_schema(); await seed(db)
    publisher = CapturingPublisher()
    runner = Beta8P1P5Runner(database=db, transport=object(), publisher=publisher,
        generation_source=Generations(), output_root=tmp_path / "artifacts",
        pipeline_factory=Incomplete)
    with pytest.raises(WritingStopped, match="scored_v1"):
        await runner.run("version", "owner")
    assert publisher.calls == []
    await db.dispose()


def test_request_ledger_is_materialized_as_runtime_metrics() -> None:
    from audio_memory.analysis.beta8_p1_p5_runner import Beta8P1P5Runner

    rows = [
        {
            "stage": "P1", "status": "response_received",
            "started_at": "2026-09-11T00:00:00+00:00",
            "finished_at": "2026-09-11T00:00:02+00:00",
            "payload": {"model": "deepseek-v4-pro"},
            "response": {"usage": {"prompt_tokens": 10, "completion_tokens": 4}},
        },
        {
            "stage": "P4", "status": "dispatching",
            "started_at": "2026-09-11T00:00:02+00:00",
            "failed_at": "2026-09-11T00:00:05+00:00",
            "payload": {"model": "deepseek-v4-pro"},
        },
        {
            "stage": "P4", "status": "response_received",
            "started_at": "2026-09-11T00:01:00+00:00",
            "finished_at": "2026-09-11T00:01:01+00:00",
            "payload": {"model": "deepseek-v4-pro"},
            "response": {"usage": {"prompt_tokens": 8, "completion_tokens": 3}},
        },
    ]
    result = {
        "new_request_count": 1,
        "metrics": {
            "model_request_count": 3, "search_model_request_count": 0,
            "search_tool_call_count": 0, "input_tokens": None,
            "output_tokens": None,
        },
    }

    metrics = Beta8P1P5Runner._metrics(
        result, search_degraded_reason=None, request_rows=rows,
        run_id="version-retry-1", provider_id="deepseek",
    )

    assert metrics.model_call_count == 3
    assert metrics.historical_model_call_count == 2
    assert metrics.new_model_call_count == 1
    assert metrics.run_id == "version-retry-1"
    assert metrics.run_duration_ms == 1000
    assert metrics.run_model_duration_ms == 1000
    assert metrics.model_duration_ms == 6000
    assert metrics.stage_durations_ms == {"P4": 1000, "all_scenes_v1": 1000}
    assert metrics.checkpoint_reused_stages == ("P1",)
    assert [call.duration_ms for call in metrics.model_calls] == [2000, 3000, 1000]
    assert metrics.model_calls[-1].attempt_kind == "resume"
    assert metrics.run_input_tokens == 8
    assert metrics.run_output_tokens == 3


@pytest.mark.asyncio
async def test_fake_model_user_flow_runs_p1_p2_p4_p5_and_publishes_card(tmp_path) -> None:
    from audio_memory.analysis.beta8_p1_p5_runner import Beta8P1P5Runner

    db = Database(tmp_path / "e2e.sqlite3")
    await db.create_schema(); await seed(db)
    transport = FakeModelTransport()
    runner = Beta8P1P5Runner(database=db, transport=transport,
        publisher=VersionPublisher(db), generation_source=Generations(),
        output_root=tmp_path / "artifacts")

    outcome = await runner.run("version", "owner")

    assert transport.stages == ["P1", "P2", "P4", "P5"]
    assert outcome.card_count == 1
    async with db.session() as session:
        card = await session.scalar(select(Card))
        version = await session.get(AnalysisVersion, "version")
    payload = json.loads(card.payload_json)
    assert payload["reportMarkdown"] == "# 明日行程\n\n明天去旅行。"
    assert payload["cardAssessment"]["raw_total"] == 100
    assert version.status == "completed"
    await db.dispose()
