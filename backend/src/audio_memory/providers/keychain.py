from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


ERR_SEC_SUCCESS = 0
ERR_SEC_AUTH_FAILED = -25293
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300


class KeychainStatus(StrEnum):
    CONFIGURED = "configured"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class KeychainReadResult:
    status: KeychainStatus
    secret: bytes | None = None


class KeychainAccessError(RuntimeError):
    pass


class SecurityClient(Protocol):
    def read(self, service: str, account: str) -> tuple[int, bytes | None]: ...

    def update(self, service: str, account: str, value: bytes) -> int: ...

    def add(self, service: str, account: str, value: bytes) -> int: ...


class KeychainRepository:
    SERVICE = "Audio Memory"
    ACCOUNTS = {
        "kimi": "provider:kimi",
        "deepseek": "provider:deepseek",
        "openai": "provider:openai",
    }

    def __init__(self, client: SecurityClient) -> None:
        self._client = client

    def read(self, provider_id: str) -> KeychainReadResult:
        account = self._account(provider_id)
        status, secret = self._client.read(self.SERVICE, account)
        if status == ERR_SEC_SUCCESS and secret:
            return KeychainReadResult(KeychainStatus.CONFIGURED, secret)
        if status == ERR_SEC_ITEM_NOT_FOUND:
            return KeychainReadResult(KeychainStatus.UNCONFIGURED)
        return KeychainReadResult(KeychainStatus.UNAVAILABLE)

    def replace(self, provider_id: str, candidate: bytes) -> None:
        account = self._account(provider_id)
        status = self._client.update(self.SERVICE, account, candidate)
        if status == ERR_SEC_SUCCESS:
            return
        if status != ERR_SEC_ITEM_NOT_FOUND:
            raise KeychainAccessError("Unable to update the system Keychain")

        status = self._client.add(self.SERVICE, account, candidate)
        if status == ERR_SEC_SUCCESS:
            return
        if status == ERR_SEC_DUPLICATE_ITEM:
            status = self._client.update(self.SERVICE, account, candidate)
            if status == ERR_SEC_SUCCESS:
                return
        raise KeychainAccessError("Unable to save the API Key in the system Keychain")

    def _account(self, provider_id: str) -> str:
        try:
            return self.ACCOUNTS[provider_id]
        except KeyError as exc:
            raise ValueError(f"Unsupported provider: {provider_id}") from exc


class MacSecurityClient:
    """Thin PyObjC boundary; importing it never exposes secrets to logs."""

    @staticmethod
    def _security():
        try:
            import Security
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise KeychainAccessError("macOS Security framework is unavailable") from exc
        return Security

    def read(self, service: str, account: str) -> tuple[int, bytes | None]:
        security = self._security()
        query = {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: service,
            security.kSecAttrAccount: account,
            security.kSecReturnData: True,
            security.kSecMatchLimit: security.kSecMatchLimitOne,
        }
        status, value = security.SecItemCopyMatching(query, None)
        return int(status), bytes(value) if value is not None else None

    def update(self, service: str, account: str, value: bytes) -> int:
        security = self._security()
        query = {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: service,
            security.kSecAttrAccount: account,
        }
        return int(security.SecItemUpdate(query, {security.kSecValueData: value}))

    def add(self, service: str, account: str, value: bytes) -> int:
        security = self._security()
        attributes = {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: service,
            security.kSecAttrAccount: account,
            security.kSecValueData: value,
            security.kSecAttrAccessible: (
                security.kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            ),
            security.kSecAttrSynchronizable: False,
        }
        result = security.SecItemAdd(attributes, None)
        status = result[0] if isinstance(result, tuple) else result
        return int(status)
