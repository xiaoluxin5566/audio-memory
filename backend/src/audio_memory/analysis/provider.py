from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import BaseModel, ConfigDict, Field

from audio_memory.analysis.events import request_with_one_repair
from audio_memory.analysis.parser import parse_event_map_output, parse_scene_output
from audio_memory.prompts.composer import ModelRequest
from audio_memory.providers.adapters import DeepSeekAdapter, KimiAdapter, OpenAIAdapter
from audio_memory.providers.keychain import KeychainRepository, KeychainStatus
from audio_memory.providers.types import PROVIDER_CONFIGS


class ProviderAnalysisError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retriable: bool = False,
        code: str = "model_analysis_failed",
        pause_batch: bool = False,
    ) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.code = code
        self.pause_batch = pause_batch


class ProviderAnalysisClient:
    def __init__(
        self,
        keychain: KeychainRepository,
        client: httpx.AsyncClient,
    ) -> None:
        self.keychain = keychain
        self.client = client
        self._remote_lock = asyncio.Lock()
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
        async with self._remote_lock:
            return await self._generate_serialized(
                provider_id,
                system=system,
                user=user,
                model_id=model_id,
            )

    async def _generate_serialized(
        self,
        provider_id: str,
        *,
        system: str,
        user: str,
        model_id: str | None = None,
    ) -> str:
        read = self.keychain.read(provider_id)
        if read.status is not KeychainStatus.CONFIGURED or read.secret is None:
            raise ProviderAnalysisError(
                "Provider credential is unavailable",
                code="keychain_unavailable",
                pause_batch=True,
            )
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
                "Provider network request failed",
                retriable=True,
                code="network_timeout",
            ) from exc
        if response.status_code == 402:
            raise ProviderAnalysisError(
                "Provider account balance is unavailable",
                code="insufficient_balance",
                pause_batch=True,
            )
        if response.status_code in {401, 403}:
            raise ProviderAnalysisError(
                "Provider credential or account is unavailable",
                code="authentication_failed",
                pause_batch=True,
            )
        if response.status_code == 429:
            raise ProviderAnalysisError(
                "Provider is temporarily unavailable",
                retriable=True,
                code="rate_limited",
                pause_batch=True,
            )
        if response.status_code >= 500:
            raise ProviderAnalysisError(
                "Provider is temporarily unavailable",
                retriable=True,
                code="provider_unavailable",
            )
        if response.is_error:
            raise ProviderAnalysisError(
                "Provider rejected the analysis request", code="content_rejected"
            )
        try:
            body = response.json()
            return self.adapters[provider_id].extract_text(body)
        except (ValueError, TypeError) as exc:
            raise ProviderAnalysisError(
                "Provider returned an invalid response", code="model_response_invalid"
            ) from exc


class RemoteSceneAnalyzer:
    def __init__(self, client: ProviderAnalysisClient) -> None:
        self.client = client

    async def analyze_event_map(self, request, provider_snapshot):
        return await request_with_one_repair(
            client=self.client,
            request=request,
            provider_snapshot=provider_snapshot,
            parse=parse_event_map_output,
        )

    async def analyze_scene(self, scene_id, request, provider_snapshot):
        return await request_with_one_repair(
            client=self.client,
            request=request,
            provider_snapshot=provider_snapshot,
            parse=lambda raw: parse_scene_output(raw, expected_scene=scene_id),
        )


class ProfileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_id: str
    dimension: str
    value: dict[str, object]
    confidence: float = Field(ge=0, le=1)
    explicit: bool
    evidence_segment_ids: list[str] = Field(min_length=1)


class ProfileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[ProfileItem]


class RemoteProfileExtractor:
    def __init__(self, client: ProviderAnalysisClient) -> None:
        self.client = client

    async def extract(self, transcript, existing, provider_snapshot):
        schema = json.dumps(ProfileEnvelope.model_json_schema(), ensure_ascii=False)
        request = ModelRequest(
            scene_id="profile",
            prompt_version=0,
            schema_version=1,
            system_rules=(
                "从音频转写中提取属于用户本人的长期画像事实。不要把其他说话人的属性归给用户。"
                "只返回符合 Schema 的 JSON；证据不足则 facts 为空。"
            ),
            common_rules="画像仅可来源于结构化转写证据，不得猜测。",
            scene_prompt="提取长期画像候选",
            user_data=json.dumps(
                {"existing_profile": existing, "transcript_data": transcript},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            schema_json=schema,
        )
        envelope = await request_with_one_repair(
            client=self.client,
            request=request,
            provider_snapshot=provider_snapshot,
            parse=ProfileEnvelope.model_validate_json,
        )
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
