from __future__ import annotations

import json
from urllib.request import Request, urlopen

from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient


def _get_json(url: str, secret: bytes) -> dict:
    request = Request(
        url,
        headers={"Authorization": f"Bearer {secret.decode('utf-8')}"},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> None:
    keychain = KeychainRepository(MacSecurityClient())
    kimi_key = keychain.read("kimi").secret
    deepseek_key = keychain.read("deepseek").secret
    if not kimi_key or not deepseek_key:
        raise RuntimeError("Provider credentials are unavailable")

    kimi = _get_json("https://api.moonshot.cn/v1/users/me/balance", kimi_key)
    deepseek = _get_json("https://api.deepseek.com/user/balance", deepseek_key)
    kimi_data = kimi.get("data") or {}
    deepseek_cny = next(
        item
        for item in deepseek.get("balance_infos", [])
        if item.get("currency") == "CNY"
    )
    print(
        json.dumps(
            {
                "kimi_cny": str(kimi_data.get("available_balance")),
                "deepseek_cny": str(deepseek_cny.get("total_balance")),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
