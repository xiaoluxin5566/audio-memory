import sqlite3
from json import loads
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from audio_memory.analysis.versions import AnalysisSnapshot, require_card_version
from audio_memory.db import Database, run_migrations
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    ProviderMetadata,
    Todo,
)
from audio_memory.repositories import AnalysisVersionRepository


def migration_config(database_path: Path) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def seed_v1_transcript(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, prompt_snapshot_json, staged_results_json, created_at, updated_at) "
            "VALUES ('job-1', 'transcribing', '{}', '[]', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO job_files "
            "(id, job_id, original_name, extension, size_bytes, sha256, position, temporary_path) "
            "VALUES ('file-1', 'job-1', 'a.mp3', '.mp3', 10, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0, '/tmp/a.mp3')"
        )
        connection.execute(
            "INSERT INTO transcripts "
            "(id, job_file_id, segment_index, start_ms, end_ms, text, words_json) "
            "VALUES ('transcript-1', 'file-1', 7, 0, 1000, '你好', '[]')"
        )
        connection.commit()


def test_initial_migration_creates_all_phase_one_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "audio-memory.sqlite3"

    run_migrations(database_path)
    run_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "provider_metadata",
        "analysis_jobs",
        "job_files",
        "transcripts",
        "batches",
        "cards",
        "todos",
        "qa_messages",
        "profile_facts",
        "prompt_versions",
        "temp_file_manifest",
        "feedback_index",
    }.issubset(tables)


def test_structured_transcript_migration_adds_required_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "structured.sqlite3"

    run_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        transcript_column_rows = list(
            connection.execute("PRAGMA table_info(transcripts)")
        )
        transcript_columns = {row[1] for row in transcript_column_rows}
        transcript_not_null = {row[1]: row[3] for row in transcript_column_rows}
        job_file_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(job_files)")
        }
        job_file_not_null = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(job_files)")
        }

    assert {"segment_uid", "speaker_id"} <= transcript_columns
    assert transcript_not_null["segment_uid"] == 1
    assert {
        "recording_started_at",
        "recording_time_source",
        "timezone",
        "speech_mapping_json",
    } <= job_file_columns
    assert job_file_not_null["recording_time_source"] == 1
    assert job_file_not_null["speech_mapping_json"] == 1


def test_structured_transcript_migration_backfills_stable_segment_uid(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "backfill.sqlite3"
    config = migration_config(database_path)
    command.upgrade(config, "0001")
    seed_v1_transcript(database_path)

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        segment_uid, recording_time_source, speech_mapping_json = connection.execute(
            "SELECT transcripts.segment_uid, job_files.recording_time_source, "
            "job_files.speech_mapping_json "
            "FROM transcripts JOIN job_files ON job_files.id = transcripts.job_file_id "
            "WHERE transcripts.id = 'transcript-1'"
        ).fetchone()
    assert segment_uid == "file-1:7"
    assert recording_time_source == "unknown"
    assert speech_mapping_json == "[]"


def test_structured_transcript_segment_uid_is_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "unique.sqlite3"
    config = migration_config(database_path)
    command.upgrade(config, "0001")
    seed_v1_transcript(database_path)
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO transcripts "
                "(id, job_file_id, segment_index, segment_uid, start_ms, end_ms, text, words_json) "
                "VALUES ('transcript-2', 'file-1', 8, 'file-1:7', 1000, 2000, '世界', '[]')"
            )


def test_transcript_risk_state_migration_rejects_unknown_state(tmp_path: Path) -> None:
    database_path = tmp_path / "transcript-risk-state.sqlite3"
    config = migration_config(database_path)
    command.upgrade(config, "0001")
    seed_v1_transcript(database_path)
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE transcripts SET risk_state = 'LOW_CONFIDENCE' "
                "WHERE id = 'transcript-1'"
            )


def test_analysis_version_source_and_running_attempt_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis-version-constraints.sqlite3"
    run_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row for row in connection.execute(
                "PRAGMA table_info(analysis_versions)"
            )
        }
        assert columns["source_job_id"][3] == 1
        assert columns["batch_id"][3] == 0

        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, prompt_snapshot_json, staged_results_json, created_at, updated_at) "
            "VALUES ('job-running', 'analyzing', '{}', '[]', 'now', 'now')"
        )
        values = (
            "job-running",
            "kimi",
            "moonshot-v1",
            "{}",
            "[]",
            "",
            "[]",
            "running",
            "now",
        )
        connection.execute(
            "INSERT INTO analysis_versions "
            "(id, source_job_id, provider_id, model_id, credential_generation, "
            "prompt_snapshot_json, profile_snapshot_json, fixed_rules_hash, "
            "staged_results_json, status, created_at) "
            "VALUES ('version-running-1', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO analysis_versions "
                "(id, source_job_id, provider_id, model_id, credential_generation, "
                "prompt_snapshot_json, profile_snapshot_json, fixed_rules_hash, "
                "staged_results_json, status, created_at) "
                "VALUES ('version-running-2', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                values,
            )


@pytest.mark.asyncio
async def test_only_one_provider_can_be_active(tmp_path: Path) -> None:
    database = Database(tmp_path / "active.sqlite3")
    await database.create_schema()

    try:
        async with database.session() as session:
            session.add_all(
                [
                    ProviderMetadata(provider_id="kimi", active=True),
                    ProviderMetadata(provider_id="openai", active=True),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_create_analysis_attempt_persists_an_immutable_snapshot(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "analysis-attempt.sqlite3")
    await database.create_schema()
    repository = AnalysisVersionRepository(database)
    snapshot = AnalysisSnapshot(
        provider_id="kimi",
        model_id="moonshot-v1",
        credential_generation=7,
        prompt_snapshot={"meeting": {"version": 3, "content": "新 Prompt"}},
        profile_snapshot=[{"subject_id": "user", "dimension": "interest"}],
        fixed_rules_hash="f" * 64,
    )

    try:
        async with database.session() as session:
            session.add(AnalysisJob(id="job-attempt", stage="analyzing"))
            await session.commit()

        version = await repository.create_attempt(
            source_job_id="job-attempt",
            batch_id=None,
            snapshot=snapshot,
            reanalysis_batch_id=None,
        )

        assert version.source_job_id == "job-attempt"
        assert version.batch_id is None
        assert version.status == "running"
        assert version.provider_id == "kimi"
        assert version.model_id == "moonshot-v1"
        assert version.credential_generation == 7
        assert loads(version.prompt_snapshot_json) == snapshot.prompt_snapshot
        assert loads(version.profile_snapshot_json) == snapshot.profile_snapshot
        assert version.fixed_rules_hash == "f" * 64
        assert loads(version.staged_results_json) == {}
        assert version.completed_at is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_create_analysis_attempt_rejects_empty_source_job_id(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "empty-source.sqlite3")
    await database.create_schema()
    repository = AnalysisVersionRepository(database)
    snapshot = AnalysisSnapshot(
        provider_id="kimi",
        model_id="moonshot-v1",
        credential_generation=0,
        prompt_snapshot={},
        profile_snapshot=[],
        fixed_rules_hash="f" * 64,
    )

    try:
        with pytest.raises(ValueError, match="source_job_id"):
            await repository.create_attempt(
                source_job_id="",
                batch_id=None,
                snapshot=snapshot,
                reanalysis_batch_id=None,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_create_analysis_attempt_allows_only_one_running_per_source_job(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "one-running.sqlite3")
    await database.create_schema()
    repository = AnalysisVersionRepository(database)
    snapshot = AnalysisSnapshot(
        provider_id="kimi",
        model_id="moonshot-v1",
        credential_generation=0,
        prompt_snapshot={},
        profile_snapshot=[],
        fixed_rules_hash="f" * 64,
    )

    try:
        async with database.session() as session:
            session.add(AnalysisJob(id="job-one-running", stage="analyzing"))
            await session.commit()
        await repository.create_attempt(
            source_job_id="job-one-running",
            batch_id=None,
            snapshot=snapshot,
            reanalysis_batch_id=None,
        )

        with pytest.raises(IntegrityError):
            await repository.create_attempt(
                source_job_id="job-one-running",
                batch_id=None,
                snapshot=snapshot,
                reanalysis_batch_id=None,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_mark_current_requires_completed_version_from_target_batch(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "mark-current.sqlite3")
    await database.create_schema()
    repository = AnalysisVersionRepository(database)

    try:
        async with database.session() as session:
            session.add_all(
                [
                    AnalysisJob(id="job-target", stage="completed"),
                    AnalysisJob(id="job-other", stage="completed"),
                ]
            )
            await session.commit()
            session.add_all(
                [
                    Batch(
                        id="batch-target",
                        job_id="job-target",
                        natural_date="2026-08-05",
                    ),
                    Batch(
                        id="batch-other",
                        job_id="job-other",
                        natural_date="2026-08-04",
                    ),
                ]
            )
            await session.commit()
            session.add_all(
                [
                    AnalysisVersion(
                        id="version-running",
                        source_job_id="job-target",
                        batch_id="batch-target",
                        provider_id="kimi",
                        model_id="moonshot-v1",
                        credential_generation=0,
                        prompt_snapshot_json="{}",
                        profile_snapshot_json="[]",
                        fixed_rules_hash="",
                        staged_results_json="{}",
                        status="running",
                    ),
                    AnalysisVersion(
                        id="version-other",
                        source_job_id="job-other",
                        batch_id="batch-other",
                        provider_id="kimi",
                        model_id="moonshot-v1",
                        credential_generation=0,
                        prompt_snapshot_json="{}",
                        profile_snapshot_json="[]",
                        fixed_rules_hash="",
                        staged_results_json="{}",
                        status="completed",
                    ),
                    AnalysisVersion(
                        id="version-completed",
                        source_job_id="job-target",
                        batch_id="batch-target",
                        provider_id="kimi",
                        model_id="moonshot-v1",
                        credential_generation=0,
                        prompt_snapshot_json="{}",
                        profile_snapshot_json="[]",
                        fixed_rules_hash="",
                        staged_results_json="{}",
                        status="completed",
                    ),
                ]
            )
            await session.commit()

        with pytest.raises(ValueError, match="completed"):
            await repository.mark_current(
                batch_id="batch-target", version_id="version-running"
            )
        with pytest.raises(ValueError, match="belong"):
            await repository.mark_current(
                batch_id="batch-target", version_id="version-other"
            )
        assert await repository.current_for_batch("batch-target") is None

        await repository.mark_current(
            batch_id="batch-target", version_id="version-completed"
        )

        current = await repository.current_for_batch("batch-target")
        assert current is not None
        assert current.id == "version-completed"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_versioned_card_writes_require_version_from_expected_batch(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "card-version-validation.sqlite3")
    await database.create_schema()

    try:
        async with database.session() as session:
            session.add_all(
                [
                    AnalysisJob(id="job-card-target", stage="completed"),
                    AnalysisJob(id="job-card-other", stage="completed"),
                ]
            )
            await session.commit()
            session.add_all(
                [
                    Batch(
                        id="batch-card-target",
                        job_id="job-card-target",
                        natural_date="2026-08-05",
                    ),
                    Batch(
                        id="batch-card-other",
                        job_id="job-card-other",
                        natural_date="2026-08-04",
                    ),
                ]
            )
            await session.commit()
            session.add(
                AnalysisVersion(
                    id="version-card-other",
                    source_job_id="job-card-other",
                    batch_id="batch-card-other",
                    provider_id="kimi",
                    model_id="moonshot-v1",
                    credential_generation=0,
                    prompt_snapshot_json="{}",
                    profile_snapshot_json="[]",
                    fixed_rules_hash="",
                    staged_results_json="{}",
                    status="completed",
                )
            )
            await session.commit()

        async with database.session() as session:
            with pytest.raises(ValueError, match="required"):
                await require_card_version(
                    session,
                    version_id=None,
                    expected_batch_id="batch-card-target",
                )
            with pytest.raises(LookupError, match="Unknown analysis version"):
                await require_card_version(
                    session,
                    version_id="version-card-missing",
                    expected_batch_id="batch-card-target",
                )
            with pytest.raises(ValueError, match="expected batch"):
                await require_card_version(
                    session,
                    version_id="version-card-other",
                    expected_batch_id="batch-card-target",
                )

            pending_version = AnalysisVersion(
                id="version-card-pending",
                source_job_id="job-card-target",
                batch_id="batch-card-target",
                provider_id="kimi",
                model_id="moonshot-v1",
                credential_generation=0,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="",
                staged_results_json="{}",
                status="running",
            )
            session.add(pending_version)
            await session.flush()
            validated = await require_card_version(
                session,
                version_id="version-card-pending",
                expected_batch_id="batch-card-target",
            )
            assert validated is pending_version
            await session.rollback()

        async with database.session() as session:
            assert await session.get(AnalysisVersion, "version-card-pending") is None
    finally:
        await database.dispose()


def test_migrated_todo_fingerprints_are_unique_when_non_null(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migrated-todo-fingerprints.sqlite3"
    config = migration_config(database_path)
    command.upgrade(config, "0002")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, prompt_snapshot_json, staged_results_json, created_at, updated_at) "
            "VALUES ('job-todo-index', 'completed', '{}', '[]', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, uploaded_at, natural_date) "
            "VALUES ('batch-todo-index', 'job-todo-index', 'now', '2026-08-05')"
        )
        connection.execute(
            "INSERT INTO todos "
            "(id, batch_id, text, completed, created_at) "
            "VALUES ('todo-index-1', 'batch-todo-index', 'one', 0, 'now')"
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT source_fingerprint FROM todos WHERE id = 'todo-index-1'"
        ).fetchone() == ("legacy:todo-index-1",)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO todos "
                "(id, batch_id, text, completed, created_at, source_fingerprint) "
                "VALUES ('todo-index-2', 'batch-todo-index', 'two', 0, 'now', "
                "'legacy:todo-index-1')"
            )
        connection.execute(
            "INSERT INTO todos "
            "(id, batch_id, text, completed, created_at, source_fingerprint) "
            "VALUES ('todo-index-null-1', 'batch-todo-index', 'null one', 0, 'now', NULL)"
        )
        connection.execute(
            "INSERT INTO todos "
            "(id, batch_id, text, completed, created_at, source_fingerprint) "
            "VALUES ('todo-index-null-2', 'batch-todo-index', 'null two', 0, 'now', NULL)"
        )


@pytest.mark.asyncio
async def test_fresh_schema_todo_fingerprints_are_unique_when_non_null(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "fresh-todo-fingerprints.sqlite3")
    await database.create_schema()
    try:
        async with database.session() as session:
            session.add(AnalysisJob(id="job-fresh-todo-index", stage="completed"))
            await session.commit()
            session.add(
                Batch(
                    id="batch-fresh-todo-index",
                    job_id="job-fresh-todo-index",
                    natural_date="2026-08-05",
                )
            )
            await session.commit()
            session.add_all(
                [
                    Todo(
                        id="todo-fresh-index-1",
                        batch_id="batch-fresh-todo-index",
                        text="one",
                        source_fingerprint="same",
                    ),
                    Todo(
                        id="todo-fresh-index-2",
                        batch_id="batch-fresh-todo-index",
                        text="two",
                        source_fingerprint="same",
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
            session.add_all(
                [
                    Todo(
                        id="todo-fresh-null-1",
                        batch_id="batch-fresh-todo-index",
                        text="null one",
                    ),
                    Todo(
                        id="todo-fresh-null-2",
                        batch_id="batch-fresh-todo-index",
                        text="null two",
                    ),
                ]
            )
            await session.commit()
    finally:
        await database.dispose()
