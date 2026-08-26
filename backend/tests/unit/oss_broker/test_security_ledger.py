from __future__ import annotations

from datetime import date

import pytest

from audio_memory.oss_broker.security_ledger import (
    SqliteEnrollmentLimiter,
    SqliteSecurityLedger,
)


def test_nonce_quota_and_revocation_survive_process_recreation(tmp_path) -> None:
    path = (tmp_path / "broker-security.sqlite3").resolve()
    first = SqliteSecurityLedger(path)
    first.claim(
        installation_id="ins_a",
        nonce="unique-request",
        requested_bytes=60,
        daily_bytes=100,
        day=date(2026, 8, 26),
    )

    restarted = SqliteSecurityLedger(path)
    with pytest.raises(PermissionError, match="replayed"):
        restarted.claim(
            installation_id="ins_a",
            nonce="unique-request",
            requested_bytes=0,
            daily_bytes=100,
            day=date(2026, 8, 26),
        )
    with pytest.raises(OverflowError):
        restarted.claim(
            installation_id="ins_a",
            nonce="another-request",
            requested_bytes=41,
            daily_bytes=100,
            day=date(2026, 8, 26),
        )

    restarted.revoke("ins_a")
    after_revoke = SqliteSecurityLedger(path)
    with pytest.raises(PermissionError, match="revoked"):
        after_revoke.claim(
            installation_id="ins_a",
            nonce="third-request",
            requested_bytes=0,
            daily_bytes=100,
            day=date(2026, 8, 26),
        )


def test_enrollment_limit_survives_process_recreation(tmp_path) -> None:
    path = (tmp_path / "broker-security.sqlite3").resolve()
    SqliteEnrollmentLimiter(
        ledger=SqliteSecurityLedger(path),
        max_per_source_per_hour=1,
        max_global_per_hour=2,
    ).claim(source="203.0.113.1", public_key=b"a" * 32)

    restarted = SqliteEnrollmentLimiter(
        ledger=SqliteSecurityLedger(path),
        max_per_source_per_hour=1,
        max_global_per_hour=2,
    )
    restarted.claim(source="203.0.113.1", public_key=b"a" * 32)
    with pytest.raises(OverflowError):
        restarted.claim(source="203.0.113.1", public_key=b"b" * 32)


def test_global_enrollment_circuit_breaker_and_kill_switch(tmp_path) -> None:
    path = (tmp_path / "broker-security.sqlite3").resolve()
    ledger = SqliteSecurityLedger(path)
    limiter = SqliteEnrollmentLimiter(
        ledger=ledger, max_per_source_per_hour=5, max_global_per_hour=1
    )
    limiter.claim(source="203.0.113.1", public_key=b"a" * 32)
    with pytest.raises(OverflowError, match="global"):
        limiter.claim(source="203.0.113.2", public_key=b"b" * 32)

    disabled = SqliteEnrollmentLimiter(
        ledger=ledger,
        max_per_source_per_hour=5,
        max_global_per_hour=5,
        enrollment_enabled=False,
    )
    with pytest.raises(OverflowError, match="disabled"):
        disabled.claim(source="203.0.113.3", public_key=b"c" * 32)
