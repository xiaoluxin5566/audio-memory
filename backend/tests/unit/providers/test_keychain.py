from __future__ import annotations

import pytest

from audio_memory.providers.keychain import (
    ERR_SEC_AUTH_FAILED,
    ERR_SEC_DUPLICATE_ITEM,
    ERR_SEC_ITEM_NOT_FOUND,
    ERR_SEC_SUCCESS,
    KeychainAccessError,
    KeychainRepository,
    KeychainStatus,
    MacSecurityClient,
)


class FakeSecurityClient:
    def __init__(self) -> None:
        self.read_result = (ERR_SEC_ITEM_NOT_FOUND, None)
        self.update_results: list[int] = []
        self.add_results: list[int] = []
        self.update_calls = 0

    def read(self, service: str, account: str):
        return self.read_result

    def update(self, service: str, account: str, value: bytes) -> int:
        self.update_calls += 1
        return self.update_results.pop(0)

    def add(self, service: str, account: str, value: bytes) -> int:
        return self.add_results.pop(0)


def test_item_not_found_is_the_only_unconfigured_result() -> None:
    client = FakeSecurityClient()
    repository = KeychainRepository(client)

    result = repository.read("kimi")

    assert result.status is KeychainStatus.UNCONFIGURED
    assert result.secret is None


def test_authorization_failure_is_not_misreported_as_unconfigured() -> None:
    client = FakeSecurityClient()
    client.read_result = (ERR_SEC_AUTH_FAILED, None)
    repository = KeychainRepository(client)

    result = repository.read("openai")

    assert result.status is KeychainStatus.UNAVAILABLE
    assert result.secret is None


def test_replace_retries_update_after_concurrent_duplicate_add() -> None:
    client = FakeSecurityClient()
    client.update_results = [ERR_SEC_ITEM_NOT_FOUND, ERR_SEC_SUCCESS]
    client.add_results = [ERR_SEC_DUPLICATE_ITEM]
    repository = KeychainRepository(client)

    repository.replace("deepseek", b"candidate")

    assert client.update_calls == 2


def test_replace_reports_second_missing_update_as_keychain_unavailable() -> None:
    client = FakeSecurityClient()
    client.update_results = [ERR_SEC_ITEM_NOT_FOUND, ERR_SEC_ITEM_NOT_FOUND]
    client.add_results = [ERR_SEC_DUPLICATE_ITEM]
    repository = KeychainRepository(client)

    with pytest.raises(KeychainAccessError):
        repository.replace("deepseek", b"candidate")


def test_mac_security_add_extracts_status_from_pyobjc_tuple(monkeypatch) -> None:
    class Security:
        kSecClass = "class"
        kSecClassGenericPassword = "generic"
        kSecAttrService = "service"
        kSecAttrAccount = "account"
        kSecValueData = "value"
        kSecAttrAccessible = "accessible"
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly = "when-unlocked"
        kSecAttrSynchronizable = "sync"
        received = None

        @staticmethod
        def SecItemAdd(attributes, result):
            Security.received = attributes
            return ERR_SEC_SUCCESS, None

    client = MacSecurityClient()
    monkeypatch.setattr(client, "_security", lambda: Security)

    assert client.add("Audio Memory", "provider:deepseek", b"candidate") == 0
    assert "data-protection" not in Security.received
