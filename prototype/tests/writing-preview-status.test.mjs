import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { buildSync } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const built = buildSync({ entryPoints: [fileURLToPath(new URL('../src/components/WritingV1PreviewStatus.jsx', import.meta.url))], bundle: true, format: 'esm', platform: 'node', write: false });
const { loadWritingV1PreviewStatus, WritingV1StoppedNotice } = await import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);
const completed = JSON.parse(readFileSync(new URL('../public/writing-v1-status.json', import.meta.url), 'utf8'));
const stopped = {
  status: 'stopped',
  new_card_count: 0,
  scored_card_count: 0,
  failure_summary: '生成在进入写作前停止。',
};
const render = (status) => renderToStaticMarkup(createElement(WritingV1StoppedNotice, { status }));

test('normal pages make no status read by default or when explicitly disabled', async () => {
  const fetcher = () => assert.fail('Normal pages must not read writing preview status');
  assert.equal(await loadWritingV1PreviewStatus({ fetcher }), null);
  assert.equal(await loadWritingV1PreviewStatus({ enabled: false, fetcher }), null);
  assert.equal(render(null), '');
});

test('V1 preview reads the local status and hides a completed run notice', async () => {
  const calls = [];
  const value = await loadWritingV1PreviewStatus({ enabled: true, fetcher: async (...args) => {
    calls.push(args);
    return { ok: true, json: async () => completed };
  } });
  assert.deepEqual(calls, [['/writing-v1-status.json', { method: 'GET', cache: 'no-store', credentials: 'same-origin' }]]);
  assert.equal(value, null);
  assert.equal(completed.status, 'scored_v1');
  assert.equal(completed.new_card_count, 7);
  assert.equal(completed.scored_card_count, 7);
});

test('stopped notice explicitly distinguishes this run from the historical reports', () => {
  const html = render(stopped);
  assert.match(html, /class="job-card warning"/);
  assert.match(html, /本轮 V1 尚未生成/);
  assert.match(html, /生成中途停止，暂无新报告和评分。下方显示历史报告。/);
  assert.ok(html.includes(stopped.failure_summary));
  assert.doesNotMatch(html, /result-card|单卡评分|审核通过/);
});

test('unavailable, malformed or non-stopped status never invents a failure result', async () => {
  for (const fetcher of [
    async () => { throw new Error('Read failed'); },
    async () => ({ ok: false }),
    async () => ({ ok: true, json: async () => { throw new Error('Not JSON'); } }),
    async () => ({ ok: true, json: async () => ({ ...stopped, status: 'running' }) }),
    async () => ({ ok: true, json: async () => ({ ...stopped, new_card_count: 1 }) }),
  ]) assert.equal(await loadWritingV1PreviewStatus({ enabled: true, fetcher }), null);
  assert.equal(render({ ...stopped, status: 'running' }), '');
});

test('failure summary is text and cannot inject HTML', () => {
  const html = render({ ...stopped, failure_summary: '<script>alert(1)</script>' });
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});
