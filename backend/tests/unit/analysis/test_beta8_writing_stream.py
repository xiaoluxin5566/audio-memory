from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from audio_memory.analysis.beta8_writing_transport import WritingTransport
from audio_memory.analysis.beta8_writing_store import WritingStore, WritingLimits, WritingStopped
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class TestKeychain:
    def read(self, provider_id):
        return KeychainReadResult(KeychainStatus.CONFIGURED, b"local-test-key")


def event(content=None, *, reasoning=None, finish=None, usage=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    value = {"id": "test-response", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None, hang=False):
        self.chunks, self.error, self.hang = chunks, error, hang
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error
        if self.hang:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


async def execute(tmp_path, stream, *, deadline=None):
    store = WritingStore(tmp_path, {"test": "stream"})
    limits = WritingLimits(allow_paid=True, max_requests=1, max_output_tokens=96000)
    sent, completed, checkpoints = [], [], []
    token = None

    async def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-request-id": "test-id"}, stream=stream)

    async def before(payload):
        nonlocal token
        token = store.before_request("P1", payload, limits)

    async def after(body):
        completed.append(body)
        store.after_response(token, body)

    async def progress(value):
        checkpoints.append(value)
        store.record_progress(token, value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = WritingTransport(TestKeychain(), client)
        if deadline is not None:
            transport._REPORT_TIMEOUT_SECONDS = deadline
        try:
            result = await transport.complete(stage="P1", system="json", user="完整输入", provider_id="deepseek", model_id="deepseek-v4-pro", max_output_tokens=96000, before_request=before, after_response=after, on_progress=progress)
        except BaseException as error:
            if token:
                store.record_error(token, error)
            return error, store, sent, completed, checkpoints
    return result, store, sent, completed, checkpoints


@pytest.mark.asyncio
async def test_stream_preserves_split_unicode_reasoning_finish_usage_and_full_input(tmp_path):
    encoded = event('{"标题":"你好"}')
    cut = encoded.index('你'.encode()) + 1
    stream = Chunks([b': keep-alive\n\n', event(reasoning='推理'), encoded[:cut], encoded[cut:], event(finish='stop', usage={'prompt_tokens': 17, 'completion_tokens': 31}), b'data: [DONE]\n\n'])
    result, store, sent, completed, snapshots = await execute(tmp_path, stream)
    assert result == '{"标题":"你好"}'
    assert sent[0]['stream'] is True
    assert sent[0]['stream_options']['include_usage'] is True
    assert sent[0]['messages'][1]['content'] == '完整输入'
    assert sent[0]['max_tokens'] == 96000
    assert completed[0]['choices'][0]['message']['reasoning_content'] == '推理'
    assert store.metrics()['output_tokens'] == 31
    assert snapshots[0]['diagnostics']['phase'] == 'response_headers'
    assert snapshots[-1]['diagnostics']['done_received'] is True
    assert stream.closed


@pytest.mark.asyncio
async def test_disconnect_keeps_partial_content_and_cause_but_never_resends(tmp_path):
    stream = Chunks([event(reasoning='思考'), event('{"partial":')], httpx.RemoteProtocolError('private local-test-key'))
    error, store, sent, completed, snapshots = await execute(tmp_path, stream)
    assert isinstance(error, ProviderAnalysisError)
    assert error.transport_error_type == 'RemoteProtocolError'
    assert error.partial_response == '{"partial":'
    assert len(sent) == 1 and completed == []
    row = store.requests()[0]
    assert row['status'] == 'dispatching'
    assert row['partial_response']['body']['choices'][0]['message']['content'] == '{"partial":'
    assert row['transport_diagnostics']['http_status'] == 200
    assert row['transport_diagnostics']['phase'] == 'receiving'
    assert store.metrics()['input_tokens'] is None
    assert store.metrics()['unresolved_request_count'] == 1
    assert 'local-test-key' not in json.dumps(row)
    with pytest.raises(WritingStopped):
        store.before_request('P1', sent[0], WritingLimits(allow_paid=True, max_requests=10))
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('chunks', [[event('{"ok":true}', finish='stop')], [event('{"ok":true}'), b'data: [DONE]\n\n'], [b'data: broken-json\n\n'], [event('{"ok":true}', finish='stop'), event('extra'), b'data: [DONE]\n\n']])
async def test_incomplete_or_invalid_stream_is_not_a_success_even_with_valid_json(tmp_path, chunks):
    error, store, sent, completed, snapshots = await execute(tmp_path, Chunks(chunks))
    assert isinstance(error, ProviderAnalysisError)
    assert error.code in {'model_stream_incomplete', 'model_response_invalid'}
    assert len(sent) == 1 and completed == []
    assert store.metrics()['unresolved_request_count'] == 1


@pytest.mark.asyncio
async def test_stream_length_still_fails_and_retains_real_usage(tmp_path):
    stream = Chunks([event('{"partial":', finish='length', usage={'prompt_tokens': 47, 'completion_tokens': 96}), b'data: [DONE]\n\n'])
    error, store, sent, completed, snapshots = await execute(tmp_path, stream)
    assert isinstance(error, ProviderAnalysisError) and error.code == 'model_output_truncated'
    assert store.metrics()['output_tokens'] == 96
    assert store.metrics()['unresolved_request_count'] == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_total_deadline_stops_a_hanging_connection_and_keeps_checkpoint(tmp_path):
    stream = Chunks([event('{"unfinished":')], hang=True)
    error, store, sent, completed, snapshots = await execute(tmp_path, stream, deadline=0.03)
    assert isinstance(error, ProviderAnalysisError) and error.code == 'network_timeout'
    assert error.transport_error_type == 'TotalDeadlineExceeded'
    assert error.partial_response == '{"unfinished":'
    assert len(sent) == 1 and completed == [] and stream.closed


@pytest.mark.asyncio
async def test_pipeline_binds_partial_progress_and_blocks_reopening_failed_stage(tmp_path):
    from audio_memory.analysis.beta8_writing_pipeline import WritingPipeline
    stream = Chunks([event('{"partial":')], httpx.ReadError('private'))
    requests = []
    async def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        pipeline = WritingPipeline(output_dir=tmp_path, transport=WritingTransport(TestKeychain(), client), limits=WritingLimits(allow_paid=True, max_requests=10), provider_id='deepseek', model_id='deepseek-v4-pro')
        pipeline.store = WritingStore(tmp_path, {'test': 'pipeline-stream'})
        with pytest.raises(ProviderAnalysisError):
            await pipeline._stage('P1', {'test': 'source'})
        stage = json.loads(next((tmp_path/'stages').glob('*.json')).read_text())
        assert stage['status'] == 'unresolved'
        assert stage['raw'] == '{"partial":'
        assert stage['result']['transport_error_type'] == 'ReadError'
        assert pipeline.store.requests()[0]['partial_response']['body']['choices'][0]['message']['content'] == '{"partial":'
        with pytest.raises(WritingStopped, match='unresolved'):
            await pipeline._stage('P1', {'test': 'source'})
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_cancelled_pipeline_keeps_checkpoint_and_propagates_cancellation(tmp_path):
    from audio_memory.analysis.beta8_writing_pipeline import WritingPipeline
    delivered = asyncio.Event()
    class Cancellable(Chunks):
        async def __aiter__(self):
            yield event('{"partial":')
            delivered.set()
            await asyncio.Event().wait()
    stream = Cancellable([])
    async def handle(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        pipeline = WritingPipeline(output_dir=tmp_path, transport=WritingTransport(TestKeychain(), client), limits=WritingLimits(allow_paid=True, max_requests=10), provider_id='deepseek', model_id='deepseek-v4-pro')
        pipeline.store = WritingStore(tmp_path, {'test': 'cancelled-stream'})
        task = asyncio.create_task(pipeline._stage('P1', {'test': 'source'}))
        await asyncio.wait_for(delivered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert pipeline.store.metrics()['unresolved_request_count'] == 1
        stage = json.loads(next((tmp_path/'stages').glob('*.json')).read_text())
        assert stage['status'] == 'unresolved' and stage['raw'] == '{"partial":'
        assert stage['result']['transport_error_type'] == 'CancelledError'
