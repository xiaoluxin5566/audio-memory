from __future__ import annotations

from datetime import UTC, datetime
import base64
import json

import httpx
import pytest

from audio_memory.oss_broker.app import (
    MemoryEnrollmentLimiter,
    MemoryObjectRegistry,
    create_broker_app,
)
from audio_memory.oss_broker.device_auth import (
    CredentialAuthority,
    DeviceRequestAuthorizer,
    DeviceRequestSigner,
    MemorySecurityLedger,
    generate_device_identity,
)


class FakeSigner:
    def __init__(self) -> None:
        self.uploads = 0

    async def issue_upload(self, **_kwargs):
        self.uploads += 1
        return {
            "url": "https://oss.test/upload",
            "headers": {"Content-Type": "audio/mpeg"},
            "expires_at": "2026-08-26T12:15:00Z",
        }

    async def issue_read(self, **_kwargs):
        return {"url": "https://oss.test/read", "expires_at": "2026-08-26T18:00:00Z"}

    async def delete(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_register_then_sign_upload_without_user_token() -> None:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    authority = CredentialAuthority(secret=b"credential-signing-secret-32bytes")
    device_authorizer = DeviceRequestAuthorizer(
        authority=authority,
        ledger=MemorySecurityLedger(),
        clock=lambda: now,
    )
    oss = FakeSigner()
    app = create_broker_app(
        authorizer=None,
        device_authorizer=device_authorizer,
        credential_authority=authority,
        daily_bytes=1024,
        registry=MemoryObjectRegistry(),
        signer=oss,
    )
    identity = generate_device_identity()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        enrollment = await client.post(
            "/v1/installations",
            json={
                "public_key": base64.urlsafe_b64encode(identity.public_key)
                .decode()
                .rstrip("="),
                "release": "0.1.0-beta.6",
            },
        )
        assert enrollment.status_code == 201
        credential = enrollment.json()["credential"]

        verify_body = b"{}"
        verify_headers = DeviceRequestSigner(identity, credential).headers(
            method="POST",
            path="/v1/installations/verify",
            body=verify_body,
            now=now,
            nonce="verify-request-1",
        )
        verified = await client.post(
            "/v1/installations/verify",
            content=verify_body,
            headers={"Content-Type": "application/json", **verify_headers},
        )
        assert verified.status_code == 204

        payload = {
            "content_type": "audio/mpeg",
            "size_bytes": 123,
            "sha256": "a" * 64,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signed = DeviceRequestSigner(identity, credential).headers(
            method="POST",
            path="/v1/uploads",
            body=body,
            now=now,
            nonce="upload-request-1",
        )
        response = await client.post(
            "/v1/uploads",
            content=body,
            headers={"Content-Type": "application/json", **signed},
        )

    assert response.status_code == 200
    assert response.json()["object_id"].startswith("obj_")
    assert oss.uploads == 1


@pytest.mark.asyncio
async def test_replayed_signed_upload_is_rejected_before_oss_signing() -> None:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    authority = CredentialAuthority(secret=b"credential-signing-secret-32bytes")
    device_authorizer = DeviceRequestAuthorizer(
        authority=authority,
        ledger=MemorySecurityLedger(),
        clock=lambda: now,
    )
    oss = FakeSigner()
    app = create_broker_app(
        authorizer=None,
        device_authorizer=device_authorizer,
        credential_authority=authority,
        daily_bytes=1024,
        registry=MemoryObjectRegistry(),
        signer=oss,
    )
    identity = generate_device_identity()
    credential = authority.issue(
        public_key=identity.public_key,
        release="0.1.0-beta.6",
        daily_bytes=1024,
        now=now,
    )
    body = json.dumps(
        {
            "content_type": "audio/mpeg",
            "size_bytes": 123,
            "sha256": "a" * 64,
        },
        separators=(",", ":"),
    ).encode()
    signed = DeviceRequestSigner(identity, credential).headers(
        method="POST", path="/v1/uploads", body=body, now=now, nonce="replay-request"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        headers = {"Content-Type": "application/json", **signed}
        first = await client.post("/v1/uploads", content=body, headers=headers)
        replay = await client.post("/v1/uploads", content=body, headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 401
    assert oss.uploads == 1


@pytest.mark.asyncio
async def test_trusted_forwarded_clients_do_not_share_the_proxy_enrollment_limit() -> None:
    authority = CredentialAuthority(secret=b"credential-signing-secret-32bytes")
    app = create_broker_app(
        authorizer=None,
        device_authorizer=DeviceRequestAuthorizer(
            authority=authority, ledger=MemorySecurityLedger()
        ),
        credential_authority=authority,
        enrollment_limiter=MemoryEnrollmentLimiter(max_per_source_per_hour=1),
        trust_forwarded_client=True,
        registry=MemoryObjectRegistry(),
        signer=FakeSigner(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        responses = []
        for address in ("203.0.113.10", "203.0.113.11"):
            identity = generate_device_identity()
            responses.append(await client.post(
                "/v1/installations",
                headers={"X-Forwarded-For": f"{address}, 192.0.2.20, 192.0.2.21"},
                json={
                    "public_key": base64.urlsafe_b64encode(identity.public_key)
                    .decode().rstrip("="),
                    "release": "0.1.0-beta.7",
                },
            ))

    assert [response.status_code for response in responses] == [201, 201]


@pytest.mark.asyncio
async def test_trusted_forwarded_client_rejects_invalid_original_address() -> None:
    authority = CredentialAuthority(secret=b"credential-signing-secret-32bytes")
    app = create_broker_app(
        authorizer=None,
        device_authorizer=DeviceRequestAuthorizer(
            authority=authority, ledger=MemorySecurityLedger()
        ),
        credential_authority=authority,
        enrollment_limiter=MemoryEnrollmentLimiter(max_per_source_per_hour=1),
        trust_forwarded_client=True,
        registry=MemoryObjectRegistry(),
        signer=FakeSigner(),
    )
    identity = generate_device_identity()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://broker.test"
    ) as client:
        response = await client.post(
            "/v1/installations",
            headers={"X-Forwarded-For": "forged-value, 192.0.2.21"},
            json={
                "public_key": base64.urlsafe_b64encode(identity.public_key)
                .decode().rstrip("="),
                "release": "0.1.0-beta.7",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_forwarded_client"
