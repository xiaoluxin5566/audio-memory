from __future__ import annotations

import base64
import asyncio
from dataclasses import dataclass, field
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from audio_memory.oss_broker.device_auth import (
    DeviceIdentity,
    DeviceRequestSigner,
    generate_device_identity,
    identity_from_private_key,
)
from audio_memory.providers.keychain import KeychainStatus


DEVICE_KEYCHAIN_ID = "managed_storage_device_key"
CREDENTIAL_KEYCHAIN_ID = "managed_storage_credential"


class IdentityKeychain:
    def read(self, credential_id: str): ...
    def replace(self, credential_id: str, candidate: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class ManagedStorageIdentity:
    device: DeviceIdentity
    credential: str

    def signer(self) -> DeviceRequestSigner:
        return DeviceRequestSigner(identity=self.device, credential=self.credential)


@dataclass(slots=True)
class ManagedStorageIdentityCoordinator:
    keychain: IdentityKeychain
    http_client: httpx.AsyncClient = field(repr=False)
    broker_base_url: str
    release: str
    _identity: ManagedStorageIdentity | None = field(default=None, init=False, repr=False)
    error_code: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.broker_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("OSS broker must use HTTPS")
        self.broker_base_url = self.broker_base_url.rstrip("/")

    @property
    def ready(self) -> bool:
        return self._identity is not None

    async def ensure_ready(self) -> ManagedStorageIdentity:
        key = self.keychain.read(DEVICE_KEYCHAIN_ID)
        credential = self.keychain.read(CREDENTIAL_KEYCHAIN_ID)
        device: DeviceIdentity | None = None
        if key.status is KeychainStatus.CONFIGURED and key.secret is not None:
            try:
                device = identity_from_private_key(key.secret)
            except ValueError:
                self.error_code = "managed_storage_identity_invalid"
        if (
            device is not None
            and credential.status is KeychainStatus.CONFIGURED
            and credential.secret is not None
        ):
            try:
                value = ManagedStorageIdentity(
                    device=device,
                    credential=credential.secret.decode("utf-8"),
                )
                body = b"{}"
                headers = value.signer().headers(
                    method="POST",
                    path="/v1/installations/verify",
                    body=body,
                )
                headers["Content-Type"] = "application/json"
                response = await self.http_client.post(
                    f"{self.broker_base_url}/v1/installations/verify",
                    content=body,
                    headers=headers,
                    timeout=10.0,
                )
                if response.status_code not in {401, 403}:
                    response.raise_for_status()
                    self._identity = value
                    self.error_code = None
                    return value
            except UnicodeDecodeError:
                self.error_code = "managed_storage_identity_invalid"
            except httpx.HTTPError:
                self.error_code = "managed_storage_verification_failed"
                raise

        generated_device = device is None
        if generated_device:
            device = generate_device_identity()
        try:
            response = await self.http_client.post(
                f"{self.broker_base_url}/v1/installations",
                json={
                    "public_key": base64.urlsafe_b64encode(device.public_key)
                    .decode("ascii")
                    .rstrip("="),
                    "release": self.release,
                },
                timeout=10.0,
            )
            response.raise_for_status()
            body = response.json()
            issued = body.get("credential") if isinstance(body, dict) else None
            if not isinstance(issued, str) or not issued:
                raise ValueError("missing installation credential")
            self.keychain.replace(CREDENTIAL_KEYCHAIN_ID, issued.encode("utf-8"))
            if generated_device:
                self.keychain.replace(DEVICE_KEYCHAIN_ID, device.private_key)
        except (httpx.HTTPError, ValueError, TypeError):
            self.error_code = "managed_storage_registration_failed"
            raise
        value = ManagedStorageIdentity(device=device, credential=issued)
        self._identity = value
        self.error_code = None
        return value

    def signer(self) -> DeviceRequestSigner:
        if self._identity is None:
            raise RuntimeError("managed storage identity is not ready")
        return self._identity.signer()


@dataclass(slots=True)
class ManagedStorageRuntime:
    identity: ManagedStorageIdentityCoordinator
    build_coordinator: Callable[[DeviceRequestSigner], object]
    on_ready: Callable[[], Awaitable[None]]
    _coordinator: object | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _next_retry_at: float = field(default=0.0, init=False, repr=False)
    _failures: int = field(default=0, init=False, repr=False)

    @property
    def ready(self) -> bool:
        return self._coordinator is not None

    @property
    def coordinator(self) -> object | None:
        return self._coordinator

    async def ensure_ready(self, *, force: bool = False) -> bool:
        if self.ready:
            return True
        async with self._lock:
            if self.ready:
                return True
            if not force and time.monotonic() < self._next_retry_at:
                return False
            try:
                await self.identity.ensure_ready()
                self._coordinator = self.build_coordinator(self.identity.signer())
                self._failures = 0
                self._next_retry_at = 0.0
                await self.on_ready()
                return True
            except Exception:
                self._failures += 1
                delay = min(30.0, float(2 ** min(self._failures - 1, 5)))
                self._next_retry_at = time.monotonic() + delay
                return False
