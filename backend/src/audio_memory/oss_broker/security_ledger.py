from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import sqlite3


class SqliteSecurityLedger:
    """Atomic security state for a Function Compute persistent volume."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("security ledger path must be absolute")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS broker_revocations (
                    installation_id TEXT PRIMARY KEY,
                    revoked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_nonces (
                    installation_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    day TEXT NOT NULL,
                    PRIMARY KEY (installation_id, nonce)
                );
                CREATE TABLE IF NOT EXISTS broker_daily_usage (
                    installation_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    used_bytes INTEGER NOT NULL,
                    PRIMARY KEY (installation_id, day)
                );
                CREATE TABLE IF NOT EXISTS broker_enrollments (
                    source TEXT NOT NULL,
                    hour TEXT NOT NULL,
                    public_key_sha256 TEXT NOT NULL,
                    PRIMARY KEY (source, hour, public_key_sha256)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def revoke(self, installation_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO broker_revocations "
                "(installation_id, revoked_at) VALUES (?, ?)",
                (installation_id, datetime.now(timezone.utc).isoformat()),
            )

    def claim(
        self,
        *,
        installation_id: str,
        nonce: str,
        requested_bytes: int,
        daily_bytes: int,
        day: date,
    ) -> None:
        day_text = day.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                revoked = connection.execute(
                    "SELECT 1 FROM broker_revocations WHERE installation_id = ?",
                    (installation_id,),
                ).fetchone()
                if revoked is not None:
                    raise PermissionError("installation revoked")
                try:
                    connection.execute(
                        "INSERT INTO broker_nonces (installation_id, nonce, day) "
                        "VALUES (?, ?, ?)",
                        (installation_id, nonce, day_text),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PermissionError("request replayed") from exc
                row = connection.execute(
                    "SELECT used_bytes FROM broker_daily_usage "
                    "WHERE installation_id = ? AND day = ?",
                    (installation_id, day_text),
                ).fetchone()
                used = int(row[0]) if row is not None else 0
                if used + requested_bytes > daily_bytes:
                    raise OverflowError("installation quota exceeded")
                connection.execute(
                    "INSERT INTO broker_daily_usage "
                    "(installation_id, day, used_bytes) VALUES (?, ?, ?) "
                    "ON CONFLICT(installation_id, day) DO UPDATE SET "
                    "used_bytes = excluded.used_bytes",
                    (installation_id, day_text, used + requested_bytes),
                )
                connection.execute(
                    "DELETE FROM broker_nonces WHERE day < ?",
                    (day_text,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


class SqliteEnrollmentLimiter:
    def __init__(
        self,
        *,
        ledger: SqliteSecurityLedger,
        max_per_source_per_hour: int,
        max_global_per_hour: int,
        enrollment_enabled: bool = True,
    ) -> None:
        if max_per_source_per_hour <= 0:
            raise ValueError("enrollment limit must be positive")
        if max_global_per_hour <= 0:
            raise ValueError("global enrollment limit must be positive")
        self._ledger = ledger
        self._limit = max_per_source_per_hour
        self._global_limit = max_global_per_hour
        self._enabled = enrollment_enabled

    def claim(self, *, source: str, public_key: bytes) -> None:
        if not self._enabled:
            raise OverflowError("enrollment disabled")
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        fingerprint = hashlib.sha256(public_key).hexdigest()
        with self._ledger._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    "SELECT 1 FROM broker_enrollments "
                    "WHERE source = ? AND hour = ? AND public_key_sha256 = ?",
                    (source, hour, fingerprint),
                ).fetchone()
                if exists is not None:
                    connection.commit()
                    return
                count = connection.execute(
                    "SELECT COUNT(*) FROM broker_enrollments "
                    "WHERE source = ? AND hour = ?",
                    (source, hour),
                ).fetchone()[0]
                if int(count) >= self._limit:
                    raise OverflowError("enrollment rate exceeded")
                global_count = connection.execute(
                    "SELECT COUNT(*) FROM broker_enrollments WHERE hour = ?",
                    (hour,),
                ).fetchone()[0]
                if int(global_count) >= self._global_limit:
                    raise OverflowError("global enrollment circuit open")
                connection.execute(
                    "INSERT INTO broker_enrollments "
                    "(source, hour, public_key_sha256) VALUES (?, ?, ?)",
                    (source, hour, fingerprint),
                )
                connection.execute(
                    "DELETE FROM broker_enrollments WHERE hour < ?",
                    (hour,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
