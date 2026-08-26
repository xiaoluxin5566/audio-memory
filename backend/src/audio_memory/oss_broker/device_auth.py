from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
from typing import Callable, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .app import BrokerInstallation


_MAX_CLOCK_SKEW = timedelta(minutes=5)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    private_key: bytes
    public_key: bytes


def generate_device_identity() -> DeviceIdentity:
    private = Ed25519PrivateKey.generate()
    return DeviceIdentity(
        private_key=private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ),
        public_key=private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ),
    )


def identity_from_private_key(private_key: bytes) -> DeviceIdentity:
    private = Ed25519PrivateKey.from_private_bytes(private_key)
    return DeviceIdentity(
        private_key=private_key,
        public_key=private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ),
    )


@dataclass(frozen=True, slots=True)
class InstallationCredential:
    token: str
    installation: BrokerInstallation
    public_key: bytes


class CredentialAuthority:
    def __init__(self, *, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("credential secret must be at least 32 bytes")
        self._secret = secret

    def issue(
        self,
        *,
        public_key: bytes,
        release: str,
        daily_bytes: int,
        now: datetime | None = None,
    ) -> str:
        if len(public_key) != 32:
            raise ValueError("invalid Ed25519 public key")
        if not release or len(release) > 64:
            raise ValueError("invalid release")
        if daily_bytes <= 0:
            raise ValueError("daily quota must be positive")
        issued = now or datetime.now(timezone.utc)
        fingerprint = hashlib.sha256(public_key).hexdigest()[:24]
        payload = json.dumps(
            {
                "d": daily_bytes,
                "i": f"ins_{fingerprint}",
                "issued": int(issued.timestamp()),
                "pk": _encode(public_key),
                "release": release,
                "v": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_encode(payload)}.{_encode(signature)}"

    def verify(self, token: str) -> InstallationCredential:
        try:
            payload_text, signature_text = token.split(".", 1)
            payload = _decode(payload_text)
            supplied = _decode(signature_text)
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                raise PermissionError("invalid credential")
            values = json.loads(payload)
            if values.get("v") != 1:
                raise PermissionError("unsupported credential")
            public_key = _decode(str(values["pk"]))
            if len(public_key) != 32:
                raise PermissionError("invalid public key")
            installation = BrokerInstallation(
                installation_id=str(values["i"]), daily_bytes=int(values["d"])
            )
            return InstallationCredential(token, installation, public_key)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise PermissionError("invalid credential") from exc


class SecurityLedger(Protocol):
    def claim(
        self,
        *,
        installation_id: str,
        nonce: str,
        requested_bytes: int,
        daily_bytes: int,
        day: date,
    ) -> None: ...


class MemorySecurityLedger:
    """Reference ledger. Production may replace it with a durable atomic store."""

    def __init__(self) -> None:
        self._nonces: set[tuple[str, str]] = set()
        self._usage: dict[tuple[str, date], int] = {}
        self._revoked: set[str] = set()

    def revoke(self, installation_id: str) -> None:
        self._revoked.add(installation_id)

    def claim(
        self,
        *,
        installation_id: str,
        nonce: str,
        requested_bytes: int,
        daily_bytes: int,
        day: date,
    ) -> None:
        if installation_id in self._revoked:
            raise PermissionError("installation revoked")
        nonce_key = (installation_id, nonce)
        if nonce_key in self._nonces:
            raise PermissionError("request replayed")
        usage_key = (installation_id, day)
        used = self._usage.get(usage_key, 0)
        if used + requested_bytes > daily_bytes:
            raise OverflowError("installation quota exceeded")
        self._nonces.add(nonce_key)
        self._usage[usage_key] = used + requested_bytes


def canonical_request(
    *, method: str, path: str, body: bytes, timestamp: str, nonce: str
) -> bytes:
    body_sha256 = hashlib.sha256(body).hexdigest()
    return "\n".join(
        (method.upper(), path, timestamp, nonce, body_sha256)
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DeviceRequestSigner:
    identity: DeviceIdentity
    credential: str

    def headers(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        now: datetime | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        timestamp = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        nonce_value = nonce or secrets.token_urlsafe(18)
        message = canonical_request(
            method=method,
            path=path,
            body=body,
            timestamp=timestamp,
            nonce=nonce_value,
        )
        signature = Ed25519PrivateKey.from_private_bytes(
            self.identity.private_key
        ).sign(message)
        return {
            "X-Audio-Memory-Credential": self.credential,
            "X-Audio-Memory-Timestamp": timestamp,
            "X-Audio-Memory-Nonce": nonce_value,
            "X-Audio-Memory-Signature": _encode(signature),
        }


class DeviceRequestAuthorizer:
    def __init__(
        self,
        *,
        authority: CredentialAuthority,
        ledger: SecurityLedger,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._authority = authority
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def authorize(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        requested_bytes: int,
    ) -> BrokerInstallation:
        try:
            credential = self._authority.verify(
                headers["X-Audio-Memory-Credential"]
            )
            timestamp_text = headers["X-Audio-Memory-Timestamp"]
            nonce = headers["X-Audio-Memory-Nonce"]
            signature = _decode(headers["X-Audio-Memory-Signature"])
            timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise PermissionError("timestamp has no timezone")
            now = self._clock()
            if abs(now - timestamp.astimezone(timezone.utc)) > _MAX_CLOCK_SKEW:
                raise PermissionError("request expired")
            if len(nonce) < 8 or len(nonce) > 128:
                raise PermissionError("invalid nonce")
            message = canonical_request(
                method=method,
                path=path,
                body=body,
                timestamp=timestamp_text,
                nonce=nonce,
            )
            Ed25519PublicKey.from_public_bytes(credential.public_key).verify(
                signature, message
            )
            self._ledger.claim(
                installation_id=credential.installation.installation_id,
                nonce=nonce,
                requested_bytes=requested_bytes,
                daily_bytes=credential.installation.daily_bytes,
                day=now.date(),
            )
            return credential.installation
        except InvalidSignature as exc:
            raise PermissionError("invalid request signature") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise PermissionError("invalid signed request") from exc
