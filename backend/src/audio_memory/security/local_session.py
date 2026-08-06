from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SESSION_TTL_SECONDS = 24 * 60 * 60
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
MAX_IDEMPOTENCY_RECORDS = 1000


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    state: Literal["owner", "pending", "replay", "mismatch", "capacity"]
    response: StoredResponse | None = None


class LocalSessionSecurity:
    """Durable hashes and outcomes for the browser-to-loopback trust boundary.

    Raw page-session tokens never reach storage. Completed mutation outcomes are
    durable so a browser retry after a backend restart remains at-most-once.
    """

    def __init__(
        self,
        storage: Path,
        *,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
        idempotency_ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS,
        max_idempotency_records: int = MAX_IDEMPOTENCY_RECORDS,
    ) -> None:
        self.storage = storage
        self.session_ttl_seconds = session_ttl_seconds
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.max_idempotency_records = max_idempotency_records
        self._initialized = False
        self._initialization_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self._ensure_initialized()
        return self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        self.storage.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.storage, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            with self._open_connection() as connection:
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS local_sessions (
                    token_hash TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    session_hash TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
                    status INTEGER,
                    headers_json TEXT,
                    response_body BLOB,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    PRIMARY KEY (session_hash, endpoint, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS ix_idempotency_created_at
                ON idempotency_records (created_at);
                """
                )
            self._initialized = True

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM local_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO local_sessions (token_hash, expires_at) VALUES (?, ?)",
                (self.token_hash(token), now + self.session_ttl_seconds),
            )
        return token

    def authenticate(self, token: str | None) -> str | None:
        if token is None or len(token) != 43:
            return None
        try:
            token.encode("ascii")
        except UnicodeEncodeError:
            return None
        token_hash = self.token_hash(token)
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM local_sessions WHERE expires_at <= ?", (now,))
            row = connection.execute(
                "SELECT token_hash FROM local_sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
        if row is None or not secrets.compare_digest(row["token_hash"], token_hash):
            return None
        return token_hash

    def claim(
        self,
        *,
        session_hash: str,
        endpoint: str,
        idempotency_key: str,
        body_hash: str,
    ) -> IdempotencyClaim:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM idempotency_records WHERE created_at <= ?",
                (now - self.idempotency_ttl_seconds,),
            )
            row = connection.execute(
                """
                SELECT body_hash, state, status, headers_json, response_body
                FROM idempotency_records
                WHERE session_hash = ? AND endpoint = ? AND idempotency_key = ?
                """,
                (session_hash, endpoint, idempotency_key),
            ).fetchone()
            if row is not None:
                connection.commit()
                if not secrets.compare_digest(row["body_hash"], body_hash):
                    return IdempotencyClaim("mismatch")
                if row["state"] == "pending":
                    return IdempotencyClaim("pending")
                return IdempotencyClaim(
                    "replay",
                    StoredResponse(
                        status=row["status"],
                        headers=tuple(
                            (name.encode("latin-1"), value.encode("latin-1"))
                            for name, value in json.loads(row["headers_json"])
                        ),
                        body=bytes(row["response_body"] or b""),
                    ),
                )

            count = connection.execute(
                "SELECT COUNT(*) AS count FROM idempotency_records"
            ).fetchone()["count"]
            if count >= self.max_idempotency_records:
                connection.commit()
                return IdempotencyClaim("capacity")
            connection.execute(
                """
                INSERT INTO idempotency_records (
                    session_hash, endpoint, idempotency_key, body_hash, state, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (session_hash, endpoint, idempotency_key, body_hash, now),
            )
            connection.commit()
            return IdempotencyClaim("owner")
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(
        self,
        *,
        session_hash: str,
        endpoint: str,
        idempotency_key: str,
        body_hash: str,
        response: StoredResponse,
    ) -> None:
        headers_json = json.dumps(
            [
                [name.decode("latin-1"), value.decode("latin-1")]
                for name, value in response.headers
            ],
            separators=(",", ":"),
        )
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE idempotency_records
                SET state = 'complete', status = ?, headers_json = ?,
                    response_body = ?, completed_at = ?
                WHERE session_hash = ? AND endpoint = ? AND idempotency_key = ?
                    AND body_hash = ? AND state = 'pending'
                """,
                (
                    response.status,
                    headers_json,
                    response.body,
                    time.time(),
                    session_hash,
                    endpoint,
                    idempotency_key,
                    body_hash,
                ),
            ).rowcount
        if updated != 1:
            raise RuntimeError("idempotency claim was lost before response publication")
