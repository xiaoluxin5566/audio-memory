from __future__ import annotations

import json

import httpx
import pytest

from audio_memory.asr.client import (
    VOLCANO_QUERY_ENDPOINT,
    VOLCANO_SUBMIT_ENDPOINT,
    AsrProviderError,
    VolcanoAsrClient,
    VolcanoSubmission,
)


def submission() -> VolcanoSubmission:
    return VolcanoSubmission(
        request_id="9df04a37-a29d-4db0-b58c-d39519f645e4",
        signed_url="https://private.example/audio.mp3?signature=secret",
        audio_format="mp3",
    )


def test_submission_repr_redacts_signed_url() -> None:
    assert "signature=secret" not in repr(submission())


@pytest.mark.asyncio
async def test_submit_uses_standard_model_endpoint_and_stable_request_id(
    respx_mock,
) -> None:
    route = respx_mock.post(VOLCANO_SUBMIT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"X-Api-Status-Code": "20000000", "X-Tt-Logid": "log-1"},
            json={},
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = VolcanoAsrClient(http_client)
        task_id = await client.submit(api_key=b"private-key", request=submission())

    request = route.calls[0].request
    assert task_id == submission().request_id
    assert request.headers["X-Api-Key"] == "private-key"
    assert request.headers["X-Api-Resource-Id"] == "volc.seedasr.auc"
    assert request.headers["X-Api-Request-Id"] == submission().request_id
    assert request.headers["X-Api-Sequence"] == "-1"
    assert json.loads(request.content) == {
        "user": {"uid": "audio-memory"},
        "audio": {
            "url": "https://private.example/audio.mp3?signature=secret",
            "format": "mp3",
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "show_utterances": True,
            "enable_speaker_info": True,
        },
    }


@pytest.mark.asyncio
async def test_poll_reuses_task_id_and_returns_completed_payload(respx_mock) -> None:
    payload = {
        "audio_info": {"duration": 1200},
        "result": {
            "text": "你好。",
            "utterances": [{"start_time": 0, "end_time": 1200, "text": "你好。"}],
        },
    }
    route = respx_mock.post(VOLCANO_QUERY_ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"X-Api-Status-Code": "20000000"}, json=payload
        )
    )
    async with httpx.AsyncClient() as http_client:
        result = await VolcanoAsrClient(http_client).poll(
            api_key=b"private-key", task_id=submission().request_id
        )

    assert result.completed is True
    assert result.payload == payload
    assert route.calls[0].request.headers["X-Api-Request-Id"] == submission().request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["20000001", "20000002"])
async def test_poll_processing_status_is_not_an_error(respx_mock, status: str) -> None:
    respx_mock.post(VOLCANO_QUERY_ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"X-Api-Status-Code": status}, json={}
        )
    )
    async with httpx.AsyncClient() as http_client:
        result = await VolcanoAsrClient(http_client).poll(
            api_key=b"private-key", task_id=submission().request_id
        )
    assert result.completed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "provider_status", "code", "retriable"),
    [
        (429, None, "rate_limited", True),
        (503, None, "provider_unavailable", True),
        (401, None, "invalid_api_key", False),
        (403, None, "permission_denied", False),
        (200, "45000001", "invalid_audio", False),
        (200, "55000001", "provider_unavailable", True),
    ],
)
async def test_provider_errors_are_stably_classified(
    respx_mock,
    http_status: int,
    provider_status: str | None,
    code: str,
    retriable: bool,
) -> None:
    headers = {} if provider_status is None else {"X-Api-Status-Code": provider_status}
    respx_mock.post(VOLCANO_SUBMIT_ENDPOINT).mock(
        return_value=httpx.Response(http_status, headers=headers, json={})
    )
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(AsrProviderError) as caught:
            await VolcanoAsrClient(http_client).submit(
                api_key=b"private-key", request=submission()
            )
    assert caught.value.code == code
    assert caught.value.retriable is retriable
    assert "private-key" not in repr(caught.value)
    assert "signature=secret" not in repr(caught.value)

