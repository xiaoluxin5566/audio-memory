from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from audio_memory.analysis.profile_rebuild import ProfileRebuilder
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    ProfileCandidate,
    ProfileFact,
)


async def seed_candidates(database: Database) -> list[AnalysisVersion]:
    async with database.session() as session:
        session.add_all(
            [
                AnalysisJob(id="job-1", stage="completed"),
                AnalysisJob(id="job-2", stage="completed"),
            ]
        )
        versions = [
            AnalysisVersion(
                id="version-1",
                source_job_id="job-1",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="completed",
                created_at="2026-08-01T08:00:00+00:00",
                completed_at="2026-08-01T08:10:00+00:00",
            ),
            AnalysisVersion(
                id="version-2",
                source_job_id="job-2",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="completed",
                created_at="2026-08-02T08:00:00+00:00",
                completed_at="2026-08-02T08:10:00+00:00",
            ),
        ]
        session.add_all(versions)
        await session.flush()
        session.add_all(
            [
                ProfileCandidate(
                    id="candidate-1",
                    analysis_version_id="version-1",
                    subject_id="user",
                    dimension="role",
                    value_json='{"name":"product manager"}',
                    confidence=0.8,
                    evidence_segment_ids_json='["seg_0_0"]',
                    origin="inferred",
                ),
                ProfileCandidate(
                    id="candidate-2",
                    analysis_version_id="version-2",
                    subject_id="user",
                    dimension="role",
                    value_json='{"name": "product manager"}',
                    confidence=0.95,
                    evidence_segment_ids_json='["seg_0_1"]',
                    origin="explicit",
                ),
                ProfileCandidate(
                    id="candidate-3",
                    analysis_version_id="version-2",
                    subject_id="user",
                    dimension="interest",
                    value_json='{"topic":"AI"}',
                    confidence=0.7,
                    evidence_segment_ids_json='["seg_0_2"]',
                    origin="inferred",
                ),
            ]
        )
        await session.commit()
    return versions


@pytest.mark.asyncio
async def test_rebuild_is_order_independent_and_aggregates_identical_facts(
    tmp_path,
) -> None:
    database = Database(tmp_path / "profile.sqlite3")
    await database.create_schema()
    versions = await seed_candidates(database)
    rebuilder = ProfileRebuilder(database)

    forward = await rebuilder.rebuild(versions)
    reverse = await rebuilder.rebuild(list(reversed(versions)))

    assert [fact.id for fact in forward] == [fact.id for fact in reverse]
    assert [(fact.dimension, json.loads(fact.value_json)) for fact in forward] == [
        ("interest", {"topic": "AI"}),
        ("role", {"name": "product manager"}),
    ]
    role = forward[1]
    assert role.confidence == 0.95
    assert role.evidence_count == 2
    assert role.origin == "explicit"
    assert json.loads(role.source_audio_json) == ["job-1", "job-2"]
    assert role.first_seen_at == "2026-08-01T08:00:00+00:00"
    assert role.last_seen_at == "2026-08-02T08:10:00+00:00"
    await database.dispose()


@pytest.mark.asyncio
async def test_rebuild_uses_only_the_supplied_current_versions(tmp_path) -> None:
    database = Database(tmp_path / "current-only.sqlite3")
    await database.create_schema()
    versions = await seed_candidates(database)

    facts = await ProfileRebuilder(database).rebuild([versions[1]])

    assert [(fact.dimension, fact.evidence_count) for fact in facts] == [
        ("interest", 1),
        ("role", 1),
    ]
    assert json.loads(facts[1].source_audio_json) == ["job-2"]
    await database.dispose()


@pytest.mark.asyncio
async def test_atomic_swap_rolls_back_to_old_profile_on_failure(tmp_path) -> None:
    database = Database(tmp_path / "profile-swap.sqlite3")
    await database.create_schema()
    old = ProfileFact(
        id="old-profile",
        subject_id="user",
        dimension="role",
        value_json='{"name":"old"}',
        confidence=0.9,
        source_audio_json='["job-old"]',
        first_seen_at="2026-07-01T00:00:00+00:00",
        last_seen_at="2026-07-01T00:00:00+00:00",
        evidence_count=1,
        origin="explicit",
        status="active",
    )
    async with database.session() as session:
        session.add(old)
        await session.commit()
    replacement = ProfileFact(
        id="duplicate",
        subject_id="user",
        dimension="role",
        value_json='{"name":"new"}',
        confidence=0.9,
        source_audio_json='["job-new"]',
        first_seen_at="2026-08-01T00:00:00+00:00",
        last_seen_at="2026-08-01T00:00:00+00:00",
        evidence_count=1,
        origin="explicit",
        status="active",
    )
    duplicate = ProfileFact(
        id="duplicate",
        subject_id="user",
        dimension="interest",
        value_json='{"topic":"new"}',
        confidence=0.8,
        source_audio_json='["job-new"]',
        first_seen_at="2026-08-01T00:00:00+00:00",
        last_seen_at="2026-08-01T00:00:00+00:00",
        evidence_count=1,
        origin="inferred",
        status="active",
    )

    with pytest.raises(IntegrityError):
        await ProfileRebuilder(database).swap_active([replacement, duplicate])

    async with database.session() as session:
        stored = list(await session.scalars(select(ProfileFact)))
    assert [fact.id for fact in stored] == ["old-profile"]
    assert stored[0].status == "active"
    await database.dispose()


@pytest.mark.asyncio
async def test_rebuild_preserves_legacy_fact_when_current_version_has_no_candidates(
    tmp_path,
) -> None:
    database = Database(tmp_path / "legacy-current.sqlite3")
    await database.create_schema()
    versions = await seed_candidates(database)
    async with database.session() as session:
        session.add(AnalysisJob(id="job-legacy", stage="completed"))
        legacy_version = AnalysisVersion(
            id="version-legacy",
            source_job_id="job-legacy",
            provider_id="legacy",
            model_id="legacy",
            credential_generation=0,
            prompt_snapshot_json="{}",
            profile_snapshot_json="[]",
            fixed_rules_hash="",
            staged_results_json="{}",
            status="completed",
            created_at="2026-07-01T00:00:00+00:00",
            completed_at="2026-07-01T00:10:00+00:00",
        )
        session.add(legacy_version)
        session.add(
            ProfileFact(
                id="legacy-fact",
                subject_id="user",
                dimension="preference",
                value_json='{"style":"concise"}',
                confidence=0.85,
                source_audio_json='{"job_id":"job-legacy"}',
                first_seen_at="2026-07-01T00:00:00+00:00",
                last_seen_at="2026-07-01T00:10:00+00:00",
                evidence_count=1,
                origin="explicit",
                status="active",
            )
        )
        await session.commit()

    facts = await ProfileRebuilder(database).rebuild(
        [versions[1], legacy_version]
    )

    assert [(fact.dimension, json.loads(fact.value_json)) for fact in facts] == [
        ("interest", {"topic": "AI"}),
        ("preference", {"style": "concise"}),
        ("role", {"name": "product manager"}),
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_mixed_legacy_aggregate_is_stable_across_repeated_rebuilds(
    tmp_path,
) -> None:
    database = Database(tmp_path / "legacy-repeat.sqlite3")
    await database.create_schema()
    versions = await seed_candidates(database)
    async with database.session() as session:
        session.add(AnalysisJob(id="job-legacy", stage="completed"))
        legacy_version = AnalysisVersion(
            id="version-legacy",
            source_job_id="job-legacy",
            provider_id="legacy",
            model_id="legacy",
            credential_generation=0,
            prompt_snapshot_json="{}",
            profile_snapshot_json="[]",
            fixed_rules_hash="",
            staged_results_json="{}",
            status="completed",
            created_at="2026-07-01T00:00:00+00:00",
            completed_at="2026-07-01T00:10:00+00:00",
        )
        session.add(legacy_version)
        session.add(
            ProfileFact(
                id="legacy-mixed",
                subject_id="user",
                dimension="role",
                value_json='{"name":"product manager"}',
                confidence=0.9,
                source_audio_json='["job-2","job-legacy"]',
                first_seen_at="2026-07-01T00:00:00+00:00",
                last_seen_at="2026-08-02T08:10:00+00:00",
                evidence_count=2,
                origin="explicit",
                status="active",
            )
        )
        await session.commit()
    rebuilder = ProfileRebuilder(database)
    current = [versions[1], legacy_version]

    first = await rebuilder.rebuild(current)
    await rebuilder.swap_active(first)
    second = await rebuilder.rebuild(current)

    first_role = next(fact for fact in first if fact.dimension == "role")
    second_role = next(fact for fact in second if fact.dimension == "role")
    assert first_role.evidence_count == 2
    assert second_role.evidence_count == 2
    assert json.loads(first_role.source_audio_json) == ["job-2", "job-legacy"]
    assert json.loads(second_role.source_audio_json) == ["job-2", "job-legacy"]
    await database.dispose()
