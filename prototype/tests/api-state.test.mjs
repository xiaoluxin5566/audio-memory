import assert from 'node:assert/strict'
import test from 'node:test'

import {
  normalizeFeed,
  normalizeHistory,
  normalizePrompts,
  normalizeProviders,
} from '../src/api/state.js'


test('provider API becomes existing UI provider map without exposing keys', () => {
  const normalized = normalizeProviders({ providers: [
    { provider_id: 'kimi', display_name: 'Kimi', state: 'available', active: true, last_validated_at: '2026-08-05T10:00:00Z' },
    { provider_id: 'openai', display_name: 'OpenAI', state: 'unconfigured', active: false },
  ] })

  assert.equal(normalized.activeProvider, 'kimi')
  assert.equal(normalized.providers.kimi.configured, true)
  assert.equal(normalized.providers.openai.configured, false)
  assert.equal(JSON.stringify(normalized).includes('api_key'), false)
})


test('provider API preserves keychain and rate-limit recovery state', () => {
  const normalized = normalizeProviders({ providers: [
    { provider_id: 'kimi', display_name: 'Kimi', state: 'keychain_unavailable', active: false, error_code: 'keychain_unavailable', error_message: '无法访问系统钥匙串' },
    { provider_id: 'deepseek', display_name: 'DeepSeek', state: 'unavailable', active: true, error_code: 'rate_limited', cooldown_until: '2026-08-05T10:01:00Z' },
  ] })

  assert.equal(normalized.providers.kimi.state, 'keychain_unavailable')
  assert.equal(normalized.providers.kimi.errorCode, 'keychain_unavailable')
  assert.equal(normalized.providers.deepseek.errorCode, 'rate_limited')
  assert.equal(normalized.providers.deepseek.cooldownUntil, '2026-08-05T10:01:00Z')
})


test('feed groups cards by natural day and upload batch', () => {
  const normalized = normalizeFeed({
    todos: [{ id: 't1', text: '回复客户', due_at: null, completed: false }],
    days: [{ date: '2026-08-05', cards: [
      { id: 'c1', batch_id: 'b1', scene_id: 'meeting', uploaded_at: '2026-08-05T10:00:00Z', payload: { card: { title: '评审会', summary: '确认范围' }, detail_sections: [] }, qa: [
        { role: 'user', content: '决定是什么？' },
        { role: 'assistant', content: '先做 macOS。' },
      ] },
      { id: 'c2', batch_id: 'b2', scene_id: 'growth', uploaded_at: '2026-08-05T11:00:00Z', payload: { card: { title: '表达建议', summary: '先说结论' }, detail_sections: [] } },
    ] }],
  })

  assert.equal(normalized.feed.length, 2)
  assert.equal(normalized.feed[0].id, 'b2')
  assert.equal(normalized.feed[1].cards[0].label, '会议纪要')
  assert.deepEqual(normalized.feed[1].qa.c1, [{ q: '决定是什么？', a: '先做 macOS。' }])
  assert.equal(normalized.todos[0].text, '回复客户')
})


test('history and prompts preserve backend versions', () => {
  const history = normalizeHistory({ days: [{ date: '2026-08-05', audio: [{ id: 'f1', original_name: '录音.mp3', duration_ms: 65000, uploaded_at: '2026-08-05T10:00:00Z' }] }] })
  const prompts = normalizePrompts({ prompts: [{ scene_id: 'todo', version: 4, content: '识别待办' }] })

  assert.equal(history[0].files[0].duration, '1分05秒')
  assert.equal(prompts.todo.version, 4)
  assert.equal(prompts.todo.current, '识别待办')
})
