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
    { provider_id: 'kimi', display_name: 'Kimi', model_id: 'kimi-k2.5', state: 'available', active: true, last_validated_at: '2026-08-05T10:00:00Z' },
    { provider_id: 'openai', display_name: 'OpenAI', model_id: 'gpt-5-mini', state: 'unconfigured', active: false },
  ] })

  assert.equal(normalized.activeProvider, 'kimi')
  assert.equal(normalized.providers.kimi.configured, true)
  assert.equal(normalized.providers.kimi.modelName, 'kimi-k2.5')
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
    todos: [{ id: 't1', text: '回复客户', due_at: '2026-08-04T08:00:00+00:00', completed: false, overdue: true }],
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
  assert.equal(normalized.todos[0].overdue, true)
})


test('history and prompts preserve backend versions', () => {
  const history = normalizeHistory({ days: [{ date: '2026-08-05', audio: [{ id: 'f1', original_name: '录音.mp3', duration_ms: 65000, uploaded_at: '2026-08-05T10:00:00Z' }] }] })
  const prompts = normalizePrompts({ prompts: [{ scene_id: 'todo', version: 4, content: '识别待办' }] })

  assert.equal(history[0].files[0].duration, '1分05秒')
  assert.equal(prompts.todo.version, 4)
  assert.equal(prompts.todo.current, '识别待办')
})


test('strict scene payloads become safe, event-grouped presentation cards', () => {
  const normalized = normalizeFeed({ days: [{ date: '2026-08-06', cards: [
    {
      id: 'analysis-meeting', batch_id: 'batch-1', scene_id: 'meeting', uploaded_at: '2026-08-06T10:00:00Z', qa: [],
      payload: { scene_id: 'meeting', cards: [
        { card: { title: '产品评审', summary: '确定范围' }, detail: { topic: '一期范围', background: '团队评审', participants: [], core_conclusions: [{ content: '先做桌面端' }], decisions: [], open_questions: [], meeting_todos: [], discussion_topics: [] } },
        { card: { title: '预算复盘', summary: '控制成本' }, detail: { topic: 'Q3 预算', background: '财务会议', participants: [], core_conclusions: [{ content: '压缩外包' }], decisions: [], open_questions: [], meeting_todos: [], discussion_topics: [] } },
      ] },
    },
    {
      id: 'analysis-content', batch_id: 'batch-1', scene_id: 'content', uploaded_at: '2026-08-06T10:00:00Z', qa: [],
      payload: { scene_id: 'content', cards: [{
        card: { title: '端侧 AI 内容回顾', summary: '两个独立内容事件' },
        confidence: 0.92, generation_reason: 'internal',
        detail: {
          consumed_items: [
            { display_title: '端侧 AI 访谈', introduction: '讨论本地推理。', inferred_title_hint: 'secret', evidence_segment_ids: ['seg-1'], key_points: [{ content: '低延迟' }], user_reactions: [{ content: '值得试试' }] },
            { display_title: '产品设计播客', introduction: '讨论用户研究。', inferred_title_hint: 'hidden', evidence_segment_ids: ['seg-2'], key_points: [{ content: '先访谈' }], user_reactions: [] },
          ], cross_event_insights: [{ content: '都关注真实体验', confidence: 0.8, supporting_event_ids: ['event-1', 'event-2'] }],
          recommendations: [{ title: '相关播客', creator: '某作者', introduction: '继续了解', recommendation_reason: '契合兴趣', search_query: '端侧 AI 播客', existence_confidence: 0.9 }],
          internal_interest_signals: [{ value: 'hidden profile signal' }],
        },
      }] },
    },
  ] }] })

  const cards = normalized.feed[0].cards
  assert.equal(cards.filter((card) => card.sceneId === 'meeting').length, 2)
  const contentCard = cards.find((card) => card.sceneId === 'content')
  assert.equal(contentCard.details.consumedItems.length, 2)
  assert.equal(contentCard.detailSections.filter((section) => section.eventTitle).length, 2)
  assert.equal(JSON.stringify(contentCard).includes('inferred_title_hint'), false)
  assert.equal(JSON.stringify(contentCard).includes('hidden profile signal'), false)
  assert.equal(JSON.stringify(contentCard).includes('confidence'), false)
  assert.equal(JSON.stringify(contentCard).includes('evidence_segment_ids'), false)
  assert.equal(JSON.stringify(contentCard).includes('generation_reason'), false)
})


test('strict scene cards preserve the server QA attached to their analysis item', () => {
  const normalized = normalizeFeed({ days: [{ date: '2026-08-06', cards: [{
    id: 'analysis-meeting', batch_id: 'batch-1', scene_id: 'meeting', uploaded_at: '2026-08-06T10:00:00Z',
    qa: [{ role: 'user', content: '旧会议的结论？' }, { role: 'assistant', content: '旧结论仍可查看。' }],
    payload: { scene_id: 'meeting', cards: [{ card: { title: '会议 A', summary: '摘要' }, detail: { topic: '主题', background: '背景', participants: [], core_conclusions: [], decisions: [], open_questions: [], meeting_todos: [], discussion_topics: [] } }] },
  }] }] })

  const card = normalized.feed[0].cards[0]
  assert.deepEqual(normalized.feed[0].qa[card.id], [{ q: '旧会议的结论？', a: '旧结论仍可查看。' }])
})


test('selected old detail snapshot keeps its QA while a reopened published card is empty', () => {
  const detail = { topic: '主题', background: '背景', participants: [], core_conclusions: [], decisions: [], open_questions: [], meeting_todos: [], discussion_topics: [] }
  const oldState = normalizeFeed({ days: [{ date: '2026-08-06', cards: [{
    id: 'old-version', batch_id: 'batch-1', scene_id: 'meeting', uploaded_at: '2026-08-06T10:00:00Z',
    qa: [{ role: 'user', content: '旧问题' }, { role: 'assistant', content: '旧回答' }],
    payload: { scene_id: 'meeting', cards: [{ card: { title: '旧结果', summary: '旧摘要' }, detail }] },
  }] }] })
  const selectedCard = { card: oldState.feed[0].cards[0], batch: oldState.feed[0] }
  const publishedState = normalizeFeed({ days: [{ date: '2026-08-06', cards: [{
    id: 'new-version', batch_id: 'batch-1', scene_id: 'meeting', uploaded_at: '2026-08-06T10:00:00Z', qa: [],
    payload: { scene_id: 'meeting', cards: [{ card: { title: '新结果', summary: '新摘要' }, detail }] },
  }] }] })

  assert.deepEqual(selectedCard.batch.qa[selectedCard.card.id], [{ q: '旧问题', a: '旧回答' }])
  assert.deepEqual(publishedState.feed[0].qa[publishedState.feed[0].cards[0].id], [])
})
