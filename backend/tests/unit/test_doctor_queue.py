from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "beta3_doctor_checks", PROJECT_ROOT / "scripts" / "doctor_checks.py"
)
assert SPEC is not None and SPEC.loader is not None
doctor_checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor_checks)


def queue_database(path: Path, *, wal: bool = True) -> None:
    with sqlite3.connect(path) as connection:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE analysis_jobs "
            "(id TEXT PRIMARY KEY, stage TEXT, error_code TEXT, updated_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE analysis_versions "
            "(id TEXT PRIMARY KEY, source_job_id TEXT, status TEXT, error_code TEXT, "
            "worker_owner_id TEXT, lease_expires_at TEXT, created_at TEXT, "
            "reanalysis_batch_id TEXT)"
        )


def test_queue_check_is_read_only_and_accepts_consistent_state(tmp_path: Path) -> None:
    database = tmp_path / "healthy.sqlite3"
    queue_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO analysis_jobs VALUES "
            "('job-1', 'analyzing', NULL, '2099-01-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO analysis_versions VALUES "
            "('version-1', 'job-1', 'pending', NULL, NULL, NULL, "
            "'2099-01-01T00:00:00+00:00', NULL)"
        )
    before = database.read_bytes()

    assert doctor_checks.check_queue(database) is True
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "statements",
    [
        (
            "INSERT INTO analysis_jobs VALUES "
            "('orphan', 'analyzing', NULL, '2000-01-01T00:00:00+00:00')",
        ),
        (
            "INSERT INTO analysis_versions VALUES "
            "('expired', 'job-x', 'running', NULL, 'owner', "
            "'2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00', NULL)",
        ),
        (
            "INSERT INTO analysis_versions VALUES "
            "('stale', 'job-x', 'pending', NULL, NULL, NULL, "
            "'2000-01-01T00:00:00+00:00', NULL)",
        ),
        (
            "INSERT INTO analysis_jobs VALUES "
            "('mismatch', 'failed', 'model_analysis_failed', "
            "'2000-01-01T00:00:00+00:00')",
            "INSERT INTO analysis_versions VALUES "
            "('mismatch-version', 'mismatch', 'pending', NULL, NULL, NULL, "
            "'2099-01-01T00:00:00+00:00', NULL)",
        ),
    ],
)
def test_queue_check_rejects_each_inconsistent_state(
    tmp_path: Path, statements: tuple[str, ...]
) -> None:
    database = tmp_path / "invalid.sqlite3"
    queue_database(database)
    with sqlite3.connect(database) as connection:
        for statement in statements:
            connection.execute(statement)

    assert doctor_checks.check_queue(database) is False


def test_queue_check_rejects_non_wal_database(tmp_path: Path) -> None:
    database = tmp_path / "delete-journal.sqlite3"
    queue_database(database, wal=False)

    assert doctor_checks.check_queue(database) is False


def test_queue_check_accepts_failed_history_before_healthy_retry(tmp_path: Path) -> None:
    database = tmp_path / "healthy-retry.sqlite3"
    queue_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO analysis_jobs VALUES "
            "('job-retry', 'analyzing', NULL, '2099-01-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO analysis_versions VALUES "
            "('old-failed', 'job-retry', 'failed', 'provider_error', NULL, NULL, "
            "'2098-01-01T00:00:00+00:00', NULL)"
        )
        connection.execute(
            "INSERT INTO analysis_versions VALUES "
            "('current-pending', 'job-retry', 'pending', NULL, NULL, NULL, "
            "'2099-01-01T00:00:00+00:00', NULL)"
        )

    assert doctor_checks.check_queue(database) is True
