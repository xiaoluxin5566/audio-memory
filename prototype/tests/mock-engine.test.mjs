import test from 'node:test';
import assert from 'node:assert/strict';
import {
  acceptAudioFile,
  buildMockBatch,
  validateProviderKey,
} from '../src/mockEngine.js';

test('provider validation rejects empty and explicitly invalid keys', async () => {
  assert.equal((await validateProviderKey('kimi', '')).ok, false);
  assert.equal((await validateProviderKey('openai', 'invalid-demo-key')).ok, false);
});

test('provider validation accepts a non-empty demo key', async () => {
  assert.equal((await validateProviderKey('deepseek', 'demo-valid-key')).ok, true);
});

test('only mp3 and aac extensions are accepted', () => {
  assert.equal(acceptAudioFile({ name: 'a.mp3' }).ok, true);
  assert.equal(acceptAudioFile({ name: 'b.aac' }).ok, true);
  assert.equal(acceptAudioFile({ name: 'c.wav' }).ok, false);
  assert.equal(
    acceptAudioFile({ name: 'c.wav' }).error,
    '不支持该文件格式，请上传 MP3 / AAC 格式文件',
  );
});

test('mock batch contains the five approved card scenes in order', () => {
  const result = buildMockBatch([{ name: 'demo.mp3', size: 1024 }], 'kimi', {});
  assert.deepEqual(result.cards.map((card) => card.sceneId), [
    'meeting', 'parenting', 'content', 'growth', 'inspiration',
  ]);
  assert.equal(result.providerId, 'kimi');
});
