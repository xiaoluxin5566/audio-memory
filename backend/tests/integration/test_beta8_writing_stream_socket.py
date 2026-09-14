"""Real loopback HTTP framing; deliberately incomplete chunked body, no cloud I/O."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from audio_memory.analysis.beta8_writing_pipeline import WritingPipeline
from audio_memory.analysis.beta8_writing_store import WritingStore, WritingLimits, WritingStopped
from audio_memory.analysis.beta8_writing_transport import WritingTransport
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus
from audio_memory.providers.types import PROVIDER_CONFIGS


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
async def test_real_http_chunked_response_preserves_checkpoint_without_resend(tmp_path, monkeypatch, complete):
    requests = []
    handlers = []
    async def serve(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            content_length = next(int(line.split(b':', 1)[1]) for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            requests.append(json.loads(await reader.readexactly(content_length)))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
            event = {'choices': [{'index': 0, 'delta': {'content': '{"ok":true}'}, 'finish_reason': None}]}
            frames = [('data: '+json.dumps(event)+'\n\n').encode()]
            if complete:
                final = {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 5}}
                frames += [('data: '+json.dumps(final)+'\n\n').encode(), b'data: [DONE]\n\n']
            for frame in frames:
                writer.write(f'{len(frame):x}\r\n'.encode()+frame+b'\r\n')
                await writer.drain()
            if complete:
                writer.write(b'0\r\n\r\n')
                await writer.drain()
            # Failure deliberately closes before the HTTP terminating chunk.
        finally:
            writer.close()
            await writer.wait_closed()

    class LocalKeychain:
        def read(self, provider_id):
            return KeychainReadResult(KeychainStatus.CONFIGURED, b'loopback-test-key')

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    config = {**PROVIDER_CONFIGS, 'deepseek': replace(PROVIDER_CONFIGS['deepseek'], endpoint=f'http://127.0.0.1:{port}/chat/completions')}
    monkeypatch.setattr('audio_memory.analysis.beta8_writing_transport.PROVIDER_CONFIGS', config)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            pipeline = WritingPipeline(output_dir=tmp_path, transport=WritingTransport(LocalKeychain(), client), limits=WritingLimits(allow_paid=True, max_requests=10), provider_id='deepseek', model_id='deepseek-v4-pro')
            pipeline.store = WritingStore(tmp_path, {'test': 'loopback'})
            if complete:
                assert await pipeline._stage('P1', {'test': 'local synthetic input'}) == {'ok': True}
                assert pipeline.store.metrics()['input_tokens'] == 20
                assert await pipeline._stage('P1', {'test': 'local synthetic input'}) == {'ok': True}
            else:
                with pytest.raises(ProviderAnalysisError) as caught:
                    await pipeline._stage('P1', {'test': 'local synthetic input'})
                assert caught.value.transport_error_type == 'RemoteProtocolError'
                assert caught.value.partial_response == '{"ok":true}'
                assert pipeline.store.metrics()['unresolved_request_count'] == 1
                assert pipeline.store.metrics()['output_tokens'] is None
                with pytest.raises(WritingStopped, match='unresolved'):
                    await pipeline._stage('P1', {'test': 'local synthetic input'})
            assert len(requests) == 1
            assert requests[0]['stream'] is True
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
