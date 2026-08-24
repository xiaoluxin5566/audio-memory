from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from audio_memory.asr.types import AsrValidationResult
from audio_memory.asr.types import ASR_PROVIDER_CONFIGS, AsrProviderId


VOLCANO_SUBMIT_ENDPOINT = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
)
VOLCANO_QUERY_ENDPOINT = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
)
_SUCCESS = "20000000"
_PROCESSING = frozenset({"20000001", "20000002"})
_SILENT_AUDIO = "20000003"


class AsrProviderError(RuntimeError):
    def __init__(self, code: str, *, retriable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retriable = retriable


@dataclass(frozen=True, slots=True)
class VolcanoSubmission:
    request_id: str
    signed_url: str = field(repr=False)
    audio_format: str


@dataclass(frozen=True, slots=True)
class VolcanoPollResult:
    completed: bool
    payload: dict[str, Any] | None = None


@dataclass(slots=True)
class VolcanoAsrClient:
    http_client: httpx.AsyncClient

    async def submit(self, *, api_key: bytes, request: VolcanoSubmission) -> str:
        response = await self._post(
            VOLCANO_SUBMIT_ENDPOINT,
            api_key=api_key,
            request_id=request.request_id,
            body={
                "user": {"uid": "audio-memory"},
                "audio": {
                    "url": request.signed_url,
                    "format": request.audio_format,
                },
                "request": {
                    "model_name": "bigmodel",
                    "enable_itn": True,
                    "enable_punc": True,
                    "enable_ddc": False,
                    "show_utterances": True,
                    "enable_speaker_info": True,
                },
            },
        )
        self._ensure_provider_success(response, allow_processing=False)
        return request.request_id

    async def poll(self, *, api_key: bytes, task_id: str) -> VolcanoPollResult:
        response = await self._post(
            VOLCANO_QUERY_ENDPOINT,
            api_key=api_key,
            request_id=task_id,
            body={},
        )
        status = self._ensure_provider_success(response, allow_processing=True)
        if status in _PROCESSING:
            return VolcanoPollResult(completed=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsrProviderError("protocol_error", retriable=False) from exc
        if not isinstance(payload, dict):
            raise AsrProviderError("protocol_error", retriable=False)
        if status == _SILENT_AUDIO:
            result = payload.get("result")
            if not isinstance(result, dict):
                raise AsrProviderError("protocol_error", retriable=False)
            payload = {**payload, "result": {**result, "utterances": []}}
        return VolcanoPollResult(completed=True, payload=payload)

    async def _post(
        self,
        endpoint: str,
        *,
        api_key: bytes,
        request_id: str,
        body: dict[str, Any],
    ) -> httpx.Response:
        try:
            decoded_key = api_key.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AsrProviderError("invalid_api_key", retriable=False) from exc
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": decoded_key,
            "X-Api-Resource-Id": ASR_PROVIDER_CONFIGS[
                AsrProviderId.VOLCANO
            ].resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        try:
            return await self.http_client.post(endpoint, headers=headers, json=body)
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            raise AsrProviderError("timeout", retriable=True) from exc
        except httpx.RequestError as exc:
            raise AsrProviderError("network_error", retriable=True) from exc

    @staticmethod
    def _ensure_provider_success(
        response: httpx.Response, *, allow_processing: bool
    ) -> str:
        if response.status_code == 401:
            raise AsrProviderError("invalid_api_key", retriable=False)
        if response.status_code == 403:
            raise AsrProviderError("permission_denied", retriable=False)
        if response.status_code == 429:
            raise AsrProviderError("rate_limited", retriable=True)
        if response.status_code >= 500:
            raise AsrProviderError("provider_unavailable", retriable=True)
        if response.status_code >= 400:
            raise AsrProviderError("request_rejected", retriable=False)

        status = response.headers.get("X-Api-Status-Code")
        if status == _SUCCESS or (
            allow_processing and status in _PROCESSING | {_SILENT_AUDIO}
        ):
            return status
        if status is not None and status.startswith("450"):
            raise AsrProviderError("invalid_audio", retriable=False)
        if status is not None and status.startswith("550"):
            raise AsrProviderError("provider_unavailable", retriable=True)
        raise AsrProviderError("protocol_error", retriable=False)


@dataclass(slots=True)
class VolcanoCredentialValidator:
    """Validate credentials with a query for a guaranteed-unknown task ID."""

    client: VolcanoAsrClient

    async def validate(self, candidate: bytes) -> AsrValidationResult:
        task_id = "00000000-0000-4000-8000-000000000000"
        try:
            await self.client.poll(api_key=candidate, task_id=task_id)
        except AsrProviderError as exc:
            if exc.code == "invalid_audio":
                # A provider-level unknown-task response proves authentication was
                # accepted without creating a billable transcription task.
                return AsrValidationResult(ok=True)
            return AsrValidationResult(ok=False, error_code=exc.code)
        return AsrValidationResult(ok=True)
