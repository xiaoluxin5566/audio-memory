from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from audio_memory.analysis.parser import SceneOutputError, parse_scene_output
from audio_memory.providers.adapters import DeepSeekAdapter, KimiAdapter, OpenAIAdapter
from audio_memory.providers.keychain import KeychainRepository, KeychainStatus
from audio_memory.providers.types import PROVIDER_CONFIGS


class ProviderAnalysisError(RuntimeError):
    def __init__(self, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable


class ProviderAnalysisClient:
    def __init__(
        self,
        keychain: KeychainRepository,
        client: httpx.AsyncClient,
    ) -> None:
        self.keychain = keychain
        self.client = client
        self.adapters = {
            "kimi": KimiAdapter(PROVIDER_CONFIGS["kimi"]),
            "deepseek": DeepSeekAdapter(PROVIDER_CONFIGS["deepseek"]),
            "openai": OpenAIAdapter(PROVIDER_CONFIGS["openai"]),
        }

    async def generate(
        self,
        provider_id: str,
        *,
        system: str,
        user: str,
        model_id: str | None = None,
    ) -> str:
        read = self.keychain.read(provider_id)
        if read.status is not KeychainStatus.CONFIGURED or read.secret is None:
            raise ProviderAnalysisError("Provider credential is unavailable")
        last_error: ProviderAnalysisError | None = None
        for attempt in range(3):
            try:
                return await self._request(
                    provider_id, read.secret, system, user, model_id=model_id
                )
            except ProviderAnalysisError as exc:
                last_error = exc
                if not exc.retriable or attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise last_error or ProviderAnalysisError("Provider request failed")

    async def _request(
        self,
        provider_id: str,
        secret: bytes,
        system: str,
        user: str,
        *,
        model_id: str | None,
    ) -> str:
        config = PROVIDER_CONFIGS[provider_id]
        if config.api_style == "responses":
            payload = {
                "model": model_id or config.model_id,
                "instructions": system,
                "input": user,
                "store": False,
            }
        else:
            payload = {
                "model": model_id or config.model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "stream": False,
                "response_format": {"type": "json_object"},
            }
        try:
            response = await self.client.post(
                config.endpoint,
                headers={
                    "Authorization": f"Bearer {secret.decode('utf-8')}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderAnalysisError(
                "Provider network request failed", retriable=True
            ) from exc
        if response.status_code in {401, 402, 403}:
            raise ProviderAnalysisError("Provider credential or account is unavailable")
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderAnalysisError(
                "Provider is temporarily unavailable", retriable=True
            )
        if response.is_error:
            raise ProviderAnalysisError("Provider rejected the analysis request")
        try:
            body = response.json()
            return self.adapters[provider_id].extract_text(body)
        except (ValueError, TypeError) as exc:
            raise ProviderAnalysisError("Provider returned an invalid response") from exc


class RemoteSceneAnalyzer:
    def __init__(self, client: ProviderAnalysisClient) -> None:
        self.client = client

    async def analyze(self, scene_id, request, provider_snapshot):
        system = (
            f"{request.system_rules}\n\n场景要求：\n{request.scene_prompt}\n\n"
            f"JSON Schema：\n{request.schema_json}"
        )
        raw = await self.client.generate(
            provider_snapshot["provider_id"],
            system=system,
            user=request.user_data,
            model_id=provider_snapshot["model_id"],
        )
        try:
            return parse_scene_output(raw, expected_scene=scene_id)
        except SceneOutputError as first_error:
            repair = await self.client.generate(
                provider_snapshot["provider_id"],
                system=(
                    "修复下面的 JSON，使其严格符合给定 Schema。只返回修复后的 JSON。\n"
                    f"Schema：{request.schema_json}"
                ),
                user=f"校验错误：{first_error}\n无效 JSON：\n{raw}",
                model_id=provider_snapshot["model_id"],
            )
            return parse_scene_output(repair, expected_scene=scene_id)


class ProfileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_id: str
    dimension: str
    value: dict[str, object]
    confidence: float = Field(ge=0, le=1)
    explicit: bool


class ProfileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[ProfileItem]


class RemoteProfileExtractor:
    def __init__(self, client: ProviderAnalysisClient) -> None:
        self.client = client

    async def extract(self, transcript, existing, provider_snapshot):
        schema = json.dumps(ProfileEnvelope.model_json_schema(), ensure_ascii=False)
        raw = await self.client.generate(
            provider_snapshot["provider_id"],
            system=(
                "从音频转写中提取属于用户本人的长期画像事实。不要把其他说话人的属性归给用户。"
                "只返回符合 Schema 的 JSON；证据不足则 facts 为空。\n"
                f"Schema：{schema}"
            ),
            user=(
                f"已有画像：{json.dumps(existing, ensure_ascii=False)}\n"
                f"<transcript>\n{transcript}\n</transcript>"
            ),
            model_id=provider_snapshot["model_id"],
        )
        try:
            envelope = ProfileEnvelope.model_validate_json(raw)
        except ValidationError as exc:
            raise ProviderAnalysisError("Profile response failed schema validation") from exc
        return [item.model_dump(mode="json") for item in envelope.facts]


class RemoteQuestionAnswerer:
    def __init__(self, client: ProviderAnalysisClient, coordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    async def answer(self, *, card, transcript, profile, history, question) -> str:
        provider = await self.coordinator.snapshot_active()
        return await self.client.generate(
            provider.provider_id,
            model_id=provider.model_id,
            system=(
                "仅根据当前卡片和其来源音频转写回答用户问题。"
                "不知道时明确说明，不引用其他会议或其他上传批次。"
            ),
            user=(
                f"卡片：{json.dumps(card, ensure_ascii=False)}\n"
                f"相关用户画像：{json.dumps(profile, ensure_ascii=False)}\n"
                f"历史问答：{json.dumps(history, ensure_ascii=False)}\n"
                f"<transcript>\n{transcript}\n</transcript>\n"
                f"问题：{question}"
            ),
        )
