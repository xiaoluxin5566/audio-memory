from typing import ClassVar

from audio_memory.providers.keychain import KeychainRepository


class RecallCredentials(KeychainRepository):
    """Dedicated slots, never fall back to old public provider credentials."""

    ACCOUNTS: ClassVar[dict[str, str]] = {
        "corporate": "recall:corporate:volcengine-deepseek-v4_1-flash",
        "embedding": "recall:embedding:Qwen3-Embedding-4B",
        "feishu": "recall:feishu:user_token",
    }
