from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from audio_memory.oss_broker.device_auth import (
    CredentialAuthority,
    DeviceRequestAuthorizer,
    DeviceRequestSigner,
    MemorySecurityLedger,
    generate_device_identity,
)


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def enrolled_device(*, daily_bytes: int = 1024):
    identity = generate_device_identity()
    authority = CredentialAuthority(secret=b"credential-signing-secret-32bytes")
    credential = authority.issue(
        public_key=identity.public_key,
        release="0.1.0-beta.6",
        daily_bytes=daily_bytes,
        now=NOW,
    )
    ledger = MemorySecurityLedger()
    authorizer = DeviceRequestAuthorizer(
        authority=authority,
        ledger=ledger,
        clock=lambda: NOW,
    )
    signer = DeviceRequestSigner(identity=identity, credential=credential)
    return signer, authorizer, ledger


def test_device_credential_and_request_signature_authorize_one_installation() -> None:
    signer, authorizer, _ledger = enrolled_device()
    body = json.dumps({"size_bytes": 123}, separators=(",", ":")).encode()
    headers = signer.headers(
        method="POST",
        path="/v1/uploads",
        body=body,
        now=NOW,
        nonce="unique-request",
    )

    installation = authorizer.authorize(
        method="POST",
        path="/v1/uploads",
        body=body,
        headers=headers,
        requested_bytes=123,
    )

    assert installation.installation_id.startswith("ins_")
    assert installation.daily_bytes == 1024


@pytest.mark.parametrize("mutation", ["body", "path", "signature"])
def test_tampered_request_is_rejected(mutation: str) -> None:
    signer, authorizer, _ledger = enrolled_device()
    body = b'{"size_bytes":123}'
    path = "/v1/uploads"
    headers = signer.headers(
        method="POST", path=path, body=body, now=NOW, nonce="tamper-test"
    )
    if mutation == "body":
        body = b'{"size_bytes":124}'
    elif mutation == "path":
        path = "/v1/objects/wrong/read-url"
    else:
        headers["X-Audio-Memory-Signature"] = "A" * 86

    with pytest.raises(PermissionError):
        authorizer.authorize(
            method="POST", path=path, body=body, headers=headers, requested_bytes=0
        )


def test_expired_timestamp_and_nonce_replay_are_rejected() -> None:
    signer, authorizer, _ledger = enrolled_device()
    headers = signer.headers(
        method="DELETE",
        path="/v1/objects/obj_1",
        body=b"",
        now=NOW - timedelta(minutes=6),
        nonce="old-request",
    )
    with pytest.raises(PermissionError, match="expired"):
        authorizer.authorize(
            method="DELETE",
            path="/v1/objects/obj_1",
            body=b"",
            headers=headers,
            requested_bytes=0,
        )

    fresh = signer.headers(
        method="DELETE",
        path="/v1/objects/obj_1",
        body=b"",
        now=NOW,
        nonce="same-request",
    )
    authorizer.authorize(
        method="DELETE",
        path="/v1/objects/obj_1",
        body=b"",
        headers=fresh,
        requested_bytes=0,
    )
    with pytest.raises(PermissionError, match="replayed"):
        authorizer.authorize(
            method="DELETE",
            path="/v1/objects/obj_1",
            body=b"",
            headers=fresh,
            requested_bytes=0,
        )


def test_quota_and_revocation_are_checked_before_authorization() -> None:
    signer, authorizer, ledger = enrolled_device(daily_bytes=100)
    first = signer.headers(
        method="POST", path="/v1/uploads", body=b"{}", now=NOW, nonce="first-request"
    )
    installation = authorizer.authorize(
        method="POST",
        path="/v1/uploads",
        body=b"{}",
        headers=first,
        requested_bytes=100,
    )
    second = signer.headers(
        method="POST", path="/v1/uploads", body=b"{}", now=NOW, nonce="second-request"
    )
    with pytest.raises(OverflowError):
        authorizer.authorize(
            method="POST",
            path="/v1/uploads",
            body=b"{}",
            headers=second,
            requested_bytes=1,
        )

    ledger.revoke(installation.installation_id)
    third = signer.headers(
        method="POST", path="/v1/uploads", body=b"{}", now=NOW, nonce="third-request"
    )
    with pytest.raises(PermissionError, match="revoked"):
        authorizer.authorize(
            method="POST",
            path="/v1/uploads",
            body=b"{}",
            headers=third,
            requested_bytes=0,
        )
