import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def migration_config(database_path: Path) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def seed_version_0002_database(database_path: Path) -> Config:
    config = migration_config(database_path)
    command.upgrade(config, "0002")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, provider_id, model_id, prompt_snapshot_json, "
            "staged_results_json, created_at, updated_at) VALUES "
            "('job-legacy', 'completed', 'kimi', 'moonshot-v1', "
            "'{\"meeting\": {\"content\": \"legacy prompt\"}}', '[]', "
            "'2026-08-01T08:00:00+00:00', '2026-08-01T08:10:00+00:00')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-legacy', 'job-legacy', 'kimi', 'moonshot-v1', "
            "'2026-08-01T08:10:00+00:00', '2026-08-01')"
        )
        connection.execute(
            "INSERT INTO job_files "
            "(id, job_id, original_name, extension, size_bytes, sha256, "
            "recording_time_source, speech_mapping_json, position, temporary_path) "
            "VALUES ('file-legacy', 'job-legacy', 'legacy.mp3', '.mp3', 11, "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'unknown', '[]', 0, '/app/audio/legacy.mp3')"
        )
        connection.execute(
            "INSERT INTO transcripts "
            "(id, job_file_id, segment_index, segment_uid, start_ms, end_ms, "
            "text, words_json) VALUES "
            "('transcript-legacy', 'file-legacy', 0, 'file-legacy:0', 0, 1000, "
            "'旧转写', '[]')"
        )
        connection.executemany(
            "INSERT INTO cards (id, batch_id, scene_id, position, payload_json) "
            "VALUES (?, 'batch-legacy', ?, ?, ?)",
            [
                ('card-legacy-1', 'meeting', 0, '{"title": "旧会议"}'),
                ('card-legacy-2', 'growth', 1, '{"title": "旧建议"}'),
            ],
        )
        connection.execute(
            "INSERT INTO qa_messages "
            "(id, card_id, role, content, position, created_at) VALUES "
            "('qa-legacy', 'card-legacy-1', 'user', '这是什么？', 0, "
            "'2026-08-01T09:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO todos "
            "(id, batch_id, source_card_id, text, due_at, completed, created_at) "
            "VALUES ('todo-legacy', 'batch-legacy', 'card-legacy-1', '跟进会议', "
            "NULL, 0, '2026-08-01T08:10:00+00:00')"
        )
        connection.commit()
    return config


def test_0003_backfills_one_current_version_without_copying_source_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    config = seed_version_0002_database(database_path)

    with sqlite3.connect(database_path) as connection:
        before_source_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("analysis_jobs", "job_files", "transcripts")
        }
        before_audio_paths = list(
            connection.execute("SELECT temporary_path FROM job_files ORDER BY id")
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "analysis_versions",
            "todo_candidates",
            "todo_tombstones",
            "profile_candidates",
            "reanalysis_batches",
            "reanalysis_items",
        } <= tables

        version = connection.execute(
            "SELECT id, source_job_id, batch_id, provider_id, model_id, "
            "prompt_snapshot_json, status, completed_at "
            "FROM analysis_versions WHERE batch_id = 'batch-legacy'"
        ).fetchone()
        assert version is not None
        version_id = version[0]
        assert version[1:] == (
            "job-legacy",
            "batch-legacy",
            "kimi",
            "moonshot-v1",
            '{"meeting": {"content": "legacy prompt"}}',
            "completed",
            "2026-08-01T08:10:00+00:00",
        )
        assert connection.execute(
            "SELECT current_analysis_version_id FROM batches "
            "WHERE id = 'batch-legacy'"
        ).fetchone() == (version_id,)
        assert connection.execute(
            "SELECT analysis_version_id FROM cards ORDER BY id"
        ).fetchall() == [(version_id,), (version_id,)]
        assert connection.execute(
            "SELECT card_id FROM qa_messages WHERE id = 'qa-legacy'"
        ).fetchone() == ("card-legacy-1",)
        assert connection.execute(
            "SELECT analysis_version_id, user_edited, source_fingerprint "
            "FROM todos WHERE id = 'todo-legacy'"
        ).fetchone() == (version_id, 1, "legacy:todo-legacy")

        after_source_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("analysis_jobs", "job_files", "transcripts")
        }
        after_audio_paths = list(
            connection.execute("SELECT temporary_path FROM job_files ORDER BY id")
        )
        assert after_source_counts == before_source_counts
        assert after_audio_paths == before_audio_paths


def test_head_normalizes_all_checkpoint_payloads_and_adds_queue_priority(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkpoint-shape.sqlite3"
    config = seed_version_0002_database(database_path)

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        analysis_job_payload = connection.execute(
            "SELECT staged_results_json FROM analysis_jobs WHERE id = 'job-legacy'"
        ).fetchone()
        version_payload = connection.execute(
            "SELECT staged_results_json, priority FROM analysis_versions "
            "WHERE source_job_id = 'job-legacy'"
        ).fetchone()
        assert analysis_job_payload == ("{}",)
        assert version_payload == ("{}", 10)


def test_0005_adds_durable_generation_and_worker_lease_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "durable-owner.sqlite3"
    config = seed_version_0002_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO provider_metadata "
            "(provider_id, active, validation_status, default_model_id) "
            "VALUES ('kimi', 1, 'available', 'kimi-k2.5')"
        )
        connection.commit()
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        provider_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(provider_metadata)")
        }
        version_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(analysis_versions)")
        }
        assert provider_columns["credential_generation"][3] == 1
        assert {
            "worker_owner_id",
            "lease_expires_at",
        } <= version_columns.keys()
        assert connection.execute(
            "SELECT credential_generation FROM provider_metadata ORDER BY provider_id"
        ).fetchall() == [(0,)]

    command.downgrade(config, "0004")

    with sqlite3.connect(database_path) as connection:
        assert "credential_generation" not in {
            row[1] for row in connection.execute("PRAGMA table_info(provider_metadata)")
        }
        version_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(analysis_versions)")
        }
        assert "worker_owner_id" not in version_columns
        assert "lease_expires_at" not in version_columns


def test_0003_downgrade_restores_0002_data_and_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.sqlite3"
    config = seed_version_0002_database(database_path)
    command.upgrade(config, "head")

    command.downgrade(config, "0002")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "analysis_versions" not in tables
        assert "todo_candidates" not in tables
        assert "current_analysis_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(batches)")
        }
        assert "analysis_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(cards)")
        }
        assert connection.execute(
            "SELECT id FROM cards ORDER BY id"
        ).fetchall() == [("card-legacy-1",), ("card-legacy-2",)]
        assert connection.execute(
            "SELECT card_id FROM qa_messages WHERE id = 'qa-legacy'"
        ).fetchone() == ("card-legacy-1",)
        assert connection.execute(
            "SELECT id, text FROM todos"
        ).fetchone() == ("todo-legacy", "跟进会议")


def test_0003_backfills_exactly_one_version_for_each_legacy_batch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "multiple-batches.sqlite3"
    config = seed_version_0002_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, provider_id, model_id, prompt_snapshot_json, "
            "staged_results_json, created_at, updated_at) VALUES "
            "('job-no-prompt', 'completed', 'openai', 'gpt-5', '', '[]', "
            "'2026-07-31T08:00:00+00:00', '2026-07-31T08:10:00+00:00')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-no-prompt', 'job-no-prompt', 'openai', 'gpt-5', "
            "'2026-07-31T08:10:00+00:00', '2026-07-31')"
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        versions_per_batch = connection.execute(
            "SELECT batch_id, count(*) FROM analysis_versions "
            "GROUP BY batch_id ORDER BY batch_id"
        ).fetchall()
        assert versions_per_batch == [
            ("batch-legacy", 1),
            ("batch-no-prompt", 1),
        ]
        assert connection.execute(
            "SELECT prompt_snapshot_json FROM analysis_versions "
            "WHERE batch_id = 'batch-no-prompt'"
        ).fetchone() == ("{}",)


def test_0003_rejects_orphan_batch_before_any_schema_or_data_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "orphan-batch.sqlite3"
    config = seed_version_0002_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-orphan-z', 'job-missing-z', NULL, NULL, 'now', '2026-08-05')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-orphan-a', 'job-missing-a', NULL, NULL, 'now', '2026-08-05')"
        )
        connection.commit()

    with pytest.raises(
        RuntimeError,
        match=r"orphan.*batch-orphan-a.*batch-orphan-z",
    ):
        command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "analysis_versions" not in tables
        assert "current_analysis_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(batches)")
        }
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0002",)
        assert connection.execute(
            "SELECT id FROM batches ORDER BY id"
        ).fetchall() == [
            ("batch-legacy",),
            ("batch-orphan-a",),
            ("batch-orphan-z",),
        ]
