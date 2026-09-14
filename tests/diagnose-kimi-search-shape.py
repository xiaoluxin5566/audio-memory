#!/usr/bin/env python3
"""Inspect Kimi K2.6 built-in web-search response fields using a public query."""

from __future__ import annotations

import asyncio
import json

import httpx

from audio_memory.providers.adapters.kimi import KimiAdapter
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
from audio_memory.providers.types import PROVIDER_CONFIGS


async def main() -> None:
    adapter = KimiAdapter(PROVIDER_CONFIGS["kimi"])
    read = KeychainRepository(MacSecurityClient()).read("kimi")
    if read.secret is None:
        raise RuntimeError("Kimi credential unavailable")
    messages: list[dict[str, object]] = []
    async with httpx.AsyncClient() as client:
        for round_index in range(4):
            payload = adapter.native_search_payload(
                model_id="kimi-k2.6",
                messages=messages,
                queries=["Kimi API 联网搜索官方文档"],
            )
            messages = list(payload["messages"])
            response = await client.post(
                adapter.config.endpoint,
                headers={
                    "Authorization": f"Bearer {read.secret.decode('utf-8')}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            body = response.json()
            choice = body.get("choices", [{}])[0]
            message = choice.get("message", {})
            print(json.dumps({
                "round": round_index + 1,
                "status": response.status_code,
                "body_keys": sorted(body),
                "choice_keys": sorted(choice),
                "finish_reason": choice.get("finish_reason"),
                "message_keys": sorted(message),
                "message": message,
                "usage": body.get("usage"),
            }, ensure_ascii=False, indent=2))
            tool_messages = adapter.native_search_tool_messages(body)
            if tool_messages is None:
                print(json.dumps({
                    "parsed_citations": adapter.native_search_citations(body),
                }, ensure_ascii=False, indent=2))
                break
            messages.extend(tool_messages)


if __name__ == "__main__":
    asyncio.run(main())
