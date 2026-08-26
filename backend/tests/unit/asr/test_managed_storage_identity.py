from __future__ import annotations

import httpx
import pytest
import base64

from audio_memory.asr.device_identity import (
    ManagedStorageIdentityCoordinator,
    ManagedStorageRuntime,
)
from audio_memory.oss_broker.device_auth import generate_device_identity
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class MemoryKeychain:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.replacements: list[str] = []

    def read(self, credential_id: str) -> KeychainReadResult:
        value = self.values.get(credential_id)
        if value is None:
            return KeychainReadResult(KeychainStatus.UNCONFIGURED)
        return KeychainReadResult(KeychainStatus.CONFIGURED, value)

    def replace(self, credential_id: str, candidate: bytes) -> None:
        self.replacements.append(credential_id)
        self.values[credential_id] = candidate


@pytest.mark.asyncio
async def test_empty_keychain_bootstraps_device_without_user_input() -> None:
    keychain = MemoryKeychain()

    def enroll(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"public_key" in body
        assert b"private" not in body
        return httpx.Response(201, json={"credential": "signed-installation"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(enroll), base_url="https://broker.test"
    )
    coordinator = ManagedStorageIdentityCoordinator(
        keychain=keychain,
        http_client=client,
        broker_base_url="https://broker.test",
        release="0.1.0-beta.6",
    )

    identity = await coordinator.ensure_ready()

    assert identity.credential == "signed-installation"
    assert keychain.read("managed_storage_device_key").secret is not None
    assert keychain.read("managed_storage_credential").secret == b"signed-installation"
    assert coordinator.ready is True
    await client.aclose()


@pytest.mark.asyncio
async def test_existing_identity_does_not_register_again() -> None:
    keychain = MemoryKeychain()
    first_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(201, json={"credential": "credential-1"})
        ),
        base_url="https://broker.test",
    )
    first = ManagedStorageIdentityCoordinator(
        keychain=keychain,
        http_client=first_client,
        broker_base_url="https://broker.test",
        release="0.1.0-beta.6",
    )
    await first.ensure_ready()
    await first_client.aclose()

    second_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(204)
                if request.url.path == "/v1/installations/verify"
                else pytest.fail("existing installation must not enroll")
            )
        ),
        base_url="https://broker.test",
    )
    second = ManagedStorageIdentityCoordinator(
        keychain=keychain,
        http_client=second_client,
        broker_base_url="https://broker.test",
        release="0.1.0-beta.6",
    )

    identity = await second.ensure_ready()

    assert identity.credential == "credential-1"
    assert second.ready is True
    await second_client.aclose()


@pytest.mark.asyncio
async def test_expired_credential_renews_with_the_existing_device_key() -> None:
    keychain = MemoryKeychain()
    original = generate_device_identity()
    keychain.values["managed_storage_device_key"] = original.private_key
    keychain.values["managed_storage_credential"] = b"expired-credential"

    def renew(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/installations/verify":
            return httpx.Response(401)
        assert request.url.path == "/v1/installations"
        expected_public_key = base64.urlsafe_b64encode(original.public_key).decode().rstrip("=")
        assert request.read().decode().find(expected_public_key) >= 0
        return httpx.Response(201, json={"credential": "renewed-credential"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(renew), base_url="https://broker.test"
    )
    coordinator = ManagedStorageIdentityCoordinator(
        keychain=keychain,
        http_client=client,
        broker_base_url="https://broker.test",
        release="0.1.0-beta.7",
    )

    identity = await coordinator.ensure_ready()

    assert identity.device.private_key == original.private_key
    assert identity.credential == "renewed-credential"
    assert keychain.replacements == ["managed_storage_credential"]
    await client.aclose()


@pytest.mark.asyncio
async def test_runtime_retries_transient_registration_and_builds_coordinator() -> None:
    keychain = MemoryKeychain()
    calls = 0

    def enroll(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(201, json={"credential": "credential-after-retry"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(enroll), base_url="https://broker.test"
    )
    identity = ManagedStorageIdentityCoordinator(
        keychain=keychain,
        http_client=client,
        broker_base_url="https://broker.test",
        release="0.1.0-beta.6",
    )
    recovered = 0

    async def on_ready() -> None:
        nonlocal recovered
        recovered += 1

    runtime = ManagedStorageRuntime(
        identity=identity,
        build_coordinator=lambda signer: signer,
        on_ready=on_ready,
    )

    assert await runtime.ensure_ready(force=True) is False
    assert runtime.ready is False
    assert await runtime.ensure_ready(force=True) is True
    assert runtime.ready is True
    assert recovered == 1
    await client.aclose()
