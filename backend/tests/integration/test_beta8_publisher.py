from __future__ import annotations

from hashlib import sha256
import json

import pytest
from sqlalchemy import func, select

from audio_memory.analysis.publisher import VersionPublisher
from audio_memory.analysis.beta8_search import stable_source_id
from audio_memory.analysis.beta8_state import Beta8StageRecord, canonical_hash
from audio_memory.analysis.pipeline_identity import build_pipeline_parameters
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, Card, JobFile, Todo, Transcript
from audio_memory.prompts.beta8_pipeline_schema import (
    Beta8PublicationBundle,
    validate_publication_bundle,
)


async def seed(database: Database, *, indexed_work_ids: tuple[str, ...] | None = None) -> None:
    parameters_json = None
    parameters_fingerprint = None
    fixed_rules_hash = "a" * 64
    staged_results_json = "{}"
    if indexed_work_ids is not None:
        _, parameters_json, parameters_fingerprint = build_pipeline_parameters(
            pipeline_kind="beta8_indexed_scene_v2", provider_id="deepseek",
            model_id="deepseek-v4-pro", credential_generation=1,
            search_provider_id=None, search_model_id=None,
        )
        parameters = json.loads(parameters_json)
        fixed_rules_hash = parameters["fixed_rules_hash"]
        transcript = [{
            "segment_id": "seg_0_0", "file_id": "file-1", "file_name": "全天.mp3",
            "recording_started_at": None, "timezone": "Asia/Shanghai",
            "start_ms": 0, "end_ms": 1_000, "speaker_id": "speaker_1",
            "text": "讨论方案。",
        }]
        event_index = {
            "input_complete": True, "input_error": None,
            "primary_sessions": [{
                "session_id": unit_id,
                "activity_kind": "conversation",
                "communication_purpose": "work",
                "participation_mode": "user_present_live_interaction",
                "start_boundary": "contact_started",
                "start_boundary_evidence_segment_ids": ["seg_0_0"],
                "end_boundary": "contact_ended",
                "end_boundary_evidence_segment_ids": ["seg_0_0"],
                "ranges": [{
                    "source_file": "file-1", "start_segment_id": "seg_0_0",
                    "end_segment_id": "seg_0_0",
                }],
                "subject": "方案",
                "description": "讨论方案",
            } for unit_id in indexed_work_ids],
            "embedded_events": [],
            "coverage_ranges": ([{
                "range": {
                    "source_file": "file-1", "start_segment_id": "seg_0_0",
                    "end_segment_id": "seg_0_0",
                },
                "disposition": "session", "session_id": unit_id,
                "excluded_reason": None, "origin": "model",
            } for unit_id in indexed_work_ids] or [{
                "range": {
                    "source_file": "file-1", "start_segment_id": "seg_0_0",
                    "end_segment_id": "seg_0_0",
                },
                "disposition": "excluded", "session_id": None,
                "excluded_reason": "empty", "origin": "model",
            }]),
            "system_excluded_ranges": [],
        }
        record = Beta8StageRecord.create(
            payload=event_index, prompt_hash=fixed_rules_hash,
            transcript_fingerprint=canonical_hash(transcript), provider_generation=1,
            upstream_artifact_hash=sha256(b"").hexdigest(),
        )
        staged_results_json = json.dumps({
            "beta8_event_index": record.model_dump(mode="json")
        }, ensure_ascii=False)
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="analyzing"))
        session.add(JobFile(
            id="file-1", job_id="job-1", original_name="全天.mp3", extension=".mp3",
            size_bytes=10, sha256="a" * 64, duration_ms=10_000,
            recording_started_at=None, recording_time_source="unknown",
            timezone="Asia/Shanghai", position=0, temporary_path="/tmp/fixture.mp3",
        ))
        session.add(Transcript(
            id="transcript-1", job_file_id="file-1", segment_index=0,
            speaker_id="speaker_1", start_ms=0, end_ms=1_000, text="讨论方案。",
            words_json="[]", risk_classified=True, is_reliable=True,
        ))
        session.add(AnalysisVersion(
            id="version-1", source_job_id="job-1", provider_id="deepseek",
            model_id="deepseek-v4-pro", credential_generation=1,
            prompt_snapshot_json="{}", profile_snapshot_json="[]",
            fixed_rules_hash=fixed_rules_hash,
            staged_results_json=staged_results_json,
            pipeline_parameters_json=parameters_json,
            pipeline_parameters_fingerprint=parameters_fingerprint,
            pipeline_metrics_json="{}", status="running", worker_owner_id="worker-1",
        ))
        await session.commit()


def bundle(
    *, card_count=1, segment_id="seg_0_0", source_id=None,
    source_url="https://example.com/doc", todos=False,
    work_unit_ids=(), expected_work_unit_ids=(),
):
    cards = []
    v1 = {}
    final = {}
    for index in range(card_count):
        card_id = f"final-{index + 1}"
        markdown = f"# 卡片 {index + 1}\n\n完整摘要 {index + 1}。"
        digest = sha256(markdown.encode()).hexdigest()
        cards.append({
            "card_id": card_id, "scene_id": "work_communication", "position": index,
            "title": f"卡片 {index + 1}", "summary": f"完整摘要 {index + 1}。",
            "markdown": markdown, "source_segment_ids": [segment_id],
            "used_source_ids": [source_id] if source_id else [],
            "origin_card_ids": [f"v1-{index + 1}"], "v1_markdown_sha256": digest,
            "final_markdown_sha256": digest, "untouched": True,
            "work_communication_unit_ids": (
                list(work_unit_ids) if index == 0 else []
            ),
        })
        v1[card_id] = digest
        final[card_id] = digest
    sources = [] if source_id is None else [{
        "source_id": source_id, "title": "Official", "url": source_url,
        "publisher": "Example", "published_at": None,
        "retrieved_at": "2026-09-02T12:00:00+08:00",
        "source_type": "official_documentation", "supports": [],
    }]
    todo_candidates = [] if not todos else [{
        "source_todo_candidate_ids": ["todo-1"], "text": "确认负责人",
        "owner_type": "user", "assignee_text": None, "due_at": None,
        "due_text": None, "evidence_segment_ids": ["seg_0_0"],
    }]
    return Beta8PublicationBundle.model_validate({
        "cards": cards, "todo_candidates": todo_candidates,
        "external_sources": sources, "v1_card_hashes": v1,
        "final_card_hashes": final, "untouched_card_ids": list(v1),
        "completed_revision_task_ids": [], "search_degraded": False,
        "search_degraded_reason": None,
        "expected_work_communication_unit_ids": list(expected_work_unit_ids),
    })


def test_publication_bundle_rejects_missing_source_registry_entry() -> None:
    with pytest.raises(ValueError, match="source_missing"):
        validate_publication_bundle(bundle(source_id="source_missing").model_copy(
            update={"external_sources": []}
        ))


def test_publication_bundle_rejects_noncanonical_or_mismatched_source_registry_entries() -> None:
    canonical_id = stable_source_id("https://example.com/doc")
    with pytest.raises(ValueError, match="canonical"):
        validate_publication_bundle(bundle(
            source_id=canonical_id,
            source_url="https://example.com/doc?utm_source=campaign",
        ))
    with pytest.raises(ValueError, match="source_id"):
        validate_publication_bundle(bundle(source_id="source_not_a_url_hash"))


def test_publication_bundle_rejects_duplicate_source_id_even_when_metadata_differs() -> None:
    canonical_id = stable_source_id("https://example.com/doc")
    valid = bundle(source_id=canonical_id)
    changed_metadata = valid.external_sources[0].model_copy(update={"title": "Different title"})
    duplicate = valid.model_copy(update={
        "external_sources": [valid.external_sources[0], changed_metadata],
    })
    with pytest.raises(ValueError, match="duplicate source IDs"):
        validate_publication_bundle(duplicate)


@pytest.mark.asyncio
async def test_beta8_publish_writes_one_card_row_per_final_card(tmp_path) -> None:
    database = Database(tmp_path / "publisher.sqlite3")
    await database.create_schema(); await seed(database)
    outcome = await VersionPublisher(database).publish_beta8(
        "version-1", bundle(card_count=2), worker_owner_id="worker-1"
    )
    async with database.session() as session:
        rows = list(await session.scalars(select(Card).order_by(Card.position)))
    assert outcome.card_count == 2
    assert len(rows) == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_preserves_scene_id_and_markdown(tmp_path) -> None:
    database = Database(tmp_path / "payload.sqlite3")
    await database.create_schema(); await seed(database)
    await VersionPublisher(database).publish_beta8("version-1", bundle(), worker_owner_id="worker-1")
    async with database.session() as session:
        row = await session.scalar(select(Card))
    payload = json.loads(row.payload_json)
    assert row.scene_id == "work_communication"
    assert payload["reportMarkdown"] == "# 卡片 1\n\n完整摘要 1。"
    assert payload["cards"][0]["evidence_segment_ids"] == ["seg_0_0"]
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_preserves_p1_p5_card_assessment(tmp_path) -> None:
    database = Database(tmp_path / "assessment.sqlite3")
    await database.create_schema(); await seed(database)
    assessment = {
        "card_id": "final-1", "title": "卡片 1",
        "card_sha256": sha256("# 卡片 1\n\n完整摘要 1。".encode()).hexdigest(),
        "rubric_version": "beta8-quality-2026-09-09-scaled", "status": "scored",
        "dimensions": {"factual_accuracy": 15, "important_coverage": 15,
                       "analysis_depth": 25, "actionability": 30,
                       "expression_structure": 15},
        "raw_total": 100, "capped_reference_total": None, "deductions": [],
        "read_scope": ["activity_1"], "available_scope": "complete_activities",
        "strengths": "证据完整", "weaknesses": "无", "missing_inputs": [],
    }
    await VersionPublisher(database).publish_beta8(
        "version-1", bundle(), worker_owner_id="worker-1",
        card_assessments={"final-1": assessment},
    )
    async with database.session() as session:
        row = await session.scalar(select(Card))
    payload = json.loads(row.payload_json)
    assert payload["writingV1"] is True
    assert payload["cardAssessment"] == assessment
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_accepts_valid_work_communication_closure(tmp_path) -> None:
    database = Database(tmp_path / "work-closure.sqlite3")
    await database.create_schema(); await seed(database)
    await VersionPublisher(database).publish_beta8(
        "version-1",
        bundle(work_unit_ids=("comm-1",), expected_work_unit_ids=("comm-1",)),
        worker_owner_id="worker-1",
    )
    async with database.session() as session:
        row = await session.scalar(select(Card))
    payload = json.loads(row.payload_json)
    assert payload["reportMarkdown"] == "# 卡片 1\n\n完整摘要 1。"
    assert "comm-1" not in payload["reportMarkdown"]
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_accepts_and_preserves_search_metrics(tmp_path) -> None:
    database = Database(tmp_path / "search-metrics.sqlite3")
    await database.create_schema(); await seed(database)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        version.pipeline_metrics_json = json.dumps({
            "input_tokens": 1_200,
            "output_tokens": 300,
            "model_call_count": 4,
            "search_input_tokens": 200,
            "search_output_tokens": 50,
            "search_model_response_count": 2,
            "web_search_tool_call_count": 1,
            "web_search_performed": True,
        })
        await session.commit()

    await VersionPublisher(database).publish_beta8(
        "version-1", bundle(), worker_owner_id="worker-1"
    )
    async with database.session() as session:
        row = await session.scalar(select(Card))
    metrics = json.loads(row.payload_json)["runtimeMetrics"]
    assert metrics["search_input_tokens"] == 200
    assert metrics["search_output_tokens"] == 50
    assert metrics["search_model_response_count"] == 2
    assert metrics["web_search_tool_call_count"] == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_rejects_unknown_segment_and_source_ids(tmp_path) -> None:
    database = Database(tmp_path / "unknown.sqlite3")
    await database.create_schema(); await seed(database)
    publisher = VersionPublisher(database)
    with pytest.raises(ValueError, match="segment"):
        await publisher.publish_beta8("version-1", bundle(segment_id="missing"), worker_owner_id="worker-1")
    with pytest.raises(ValueError, match="source"):
        await publisher.publish_beta8("version-1", bundle(source_id="known").model_copy(
            update={"external_sources": []}
        ), worker_owner_id="worker-1")
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_rejects_conflicting_source_registry_before_audio_move_or_database_write(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "conflicting-source.sqlite3")
    await database.create_schema(); await seed(database)
    publisher = VersionPublisher(database)
    canonical_id = stable_source_id("https://example.com/doc")
    valid = bundle(source_id=canonical_id)
    conflicting = valid.model_copy(update={
        "external_sources": [
            valid.external_sources[0],
            valid.external_sources[0].model_copy(update={"title": "Conflicting title"}),
        ],
    })

    monkeypatch.setattr(
        publisher, "_move_first_publication_audio",
        lambda *args: (_ for _ in ()).throw(AssertionError("audio move must not run")),
    )
    with pytest.raises(ValueError, match="duplicate source IDs"):
        await publisher.publish_beta8("version-1", conflicting, worker_owner_id="worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        cards = await session.scalar(select(func.count(Card.id)))
    assert version.status == "running"
    assert cards == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_rejects_work_unit_closure_before_audio_move_or_database_write(
    tmp_path, monkeypatch,
) -> None:
    database = Database(tmp_path / "missing-work-closure.sqlite3")
    await database.create_schema(); await seed(database)
    publisher = VersionPublisher(database)
    invalid = bundle(expected_work_unit_ids=("comm-1",))
    monkeypatch.setattr(
        publisher, "_move_first_publication_audio",
        lambda *args: (_ for _ in ()).throw(AssertionError("audio move must not run")),
    )

    with pytest.raises(ValueError, match="work communication unit closure"):
        await publisher.publish_beta8(
            "version-1", invalid, worker_owner_id="worker-1"
        )
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        cards = await session.scalar(select(func.count(Card.id)))
    assert version.status == "running"
    assert cards == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_publish_derives_work_units_from_checkpoint_before_side_effects(
    tmp_path, monkeypatch,
) -> None:
    database = Database(tmp_path / "indexed-work-truth.sqlite3")
    await database.create_schema()
    await seed(database, indexed_work_ids=("comm-1",))
    publisher = VersionPublisher(database)
    monkeypatch.setattr(
        publisher, "_move_first_publication_audio",
        lambda *args: (_ for _ in ()).throw(AssertionError("audio move must not run")),
    )

    with pytest.raises(ValueError, match="event-index work communication units"):
        await publisher.publish_beta8(
            "version-1", bundle(), worker_owner_id="worker-1"
        )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        cards = await session.scalar(select(func.count(Card.id)))
    assert version.status == "running"
    assert cards == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_publish_accepts_checkpoint_bound_work_closure(tmp_path) -> None:
    database = Database(tmp_path / "indexed-work-valid.sqlite3")
    await database.create_schema()
    await seed(database, indexed_work_ids=("comm-1",))

    outcome = await VersionPublisher(database).publish_beta8(
        "version-1",
        bundle(work_unit_ids=("comm-1",), expected_work_unit_ids=("comm-1",)),
        worker_owner_id="worker-1",
    )

    assert outcome.card_count == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_publish_rejects_tampered_event_index_before_side_effects(
    tmp_path, monkeypatch,
) -> None:
    database = Database(tmp_path / "indexed-work-tampered.sqlite3")
    await database.create_schema()
    await seed(database, indexed_work_ids=("comm-1",))
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        staged = json.loads(version.staged_results_json)
        staged["beta8_event_index"]["payload"]["primary_sessions"][0]["session_id"] = "tampered"
        version.staged_results_json = json.dumps(staged, ensure_ascii=False)
        await session.commit()
    publisher = VersionPublisher(database)
    monkeypatch.setattr(
        publisher, "_move_first_publication_audio",
        lambda *args: (_ for _ in ()).throw(AssertionError("audio move must not run")),
    )

    with pytest.raises(ValueError, match="artifact_hash"):
        await publisher.publish_beta8(
            "version-1",
            bundle(work_unit_ids=("comm-1",), expected_work_unit_ids=("comm-1",)),
            worker_owner_id="worker-1",
        )

    async with database.session() as session:
        assert await session.scalar(select(func.count(Card.id))) == 0
        assert (await session.get(AnalysisVersion, "version-1")).status == "running"
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_reconciles_global_todos_once(tmp_path) -> None:
    database = Database(tmp_path / "todos.sqlite3")
    await database.create_schema(); await seed(database)
    publisher = VersionPublisher(database)
    first = await publisher.publish_beta8("version-1", bundle(todos=True), worker_owner_id="worker-1")
    second = await publisher.publish_beta8("version-1", bundle(todos=True), worker_owner_id="worker-1")
    async with database.session() as session:
        count = await session.scalar(select(func.count(Todo.id)))
    assert first.todo_count == second.todo_count == 1
    assert count == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_is_atomic_on_card_failure(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "atomic.sqlite3")
    await database.create_schema(); await seed(database)
    publisher = VersionPublisher(database)

    async def fail(*args, **kwargs):
        raise RuntimeError("card failure")

    monkeypatch.setattr(publisher, "_insert_beta8_cards", fail)
    with pytest.raises(RuntimeError, match="card failure"):
        await publisher.publish_beta8("version-1", bundle(), worker_owner_id="worker-1")
    async with database.session() as session:
        count = await session.scalar(select(func.count(Card.id)))
        version = await session.get(AnalysisVersion, "version-1")
    assert count == 0
    assert version.status == "running"
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_publish_is_idempotent_after_completion(tmp_path) -> None:
    database = Database(tmp_path / "idempotent.sqlite3")
    await database.create_schema(); await seed(database)
    publisher = VersionPublisher(database)
    first = await publisher.publish_beta8("version-1", bundle(), worker_owner_id="worker-1")
    second = await publisher.publish_beta8("version-1", bundle(), worker_owner_id="worker-1")
    async with database.session() as session:
        count = await session.scalar(select(func.count(Card.id)))
    assert first == second
    assert count == 1
    await database.dispose()
