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


test('position-zero batch overview shows its fixed title and summary', () => {
  const normalized = normalizeFeed({ days: [{ date: '2026-08-12', cards: [{
    id: 'overview-1',
    batch_id: 'batch-1',
    scene_id: 'batch_overview',
    uploaded_at: '2026-08-12T10:00:00Z',
    payload: {
      scene_id: 'batch_overview',
      kind: 'batch_overview',
      overview: {
        title: '本次概览',
        summary: '这段录音从亲子对话转向节目笔记。',
        scene_ids: ['child-transition', 'media-note'],
      },
    },
    qa: [],
  }] }] })

  assert.equal(normalized.feed[0].cards[0].title, '本次概览')
  assert.equal(
    normalized.feed[0].cards[0].summary,
    '这段录音从亲子对话转向节目笔记。',
  )
})


test('history and prompts preserve runtime prompt metadata', () => {
  const history = normalizeHistory({ days: [{ date: '2026-08-05', audio: [{ id: 'f1', original_name: '录音.mp3', duration_ms: 65000, uploaded_at: '2026-08-05T10:00:00Z' }] }] })
  const prompts = normalizePrompts({ prompts: [{ scene_id: 'autonomous-analysis', label: '自主分析', version: 2, content: '完整分析', editable: false, source: 'versioned-code' }] })

  assert.equal(history[0].files[0].duration, '1分05秒')
  assert.equal(prompts['autonomous-analysis'].version, 2)
  assert.equal(prompts['autonomous-analysis'].current, '完整分析')
  assert.equal(prompts['autonomous-analysis'].label, '自主分析')
  assert.equal(prompts['autonomous-analysis'].editable, false)
})


test('strict scene payloads become safe, event-grouped presentation cards', () => {
  const normalized = normalizeFeed({ days: [{ date: '2026-08-06', cards: [
    {
      id: 'analysis-meeting', batch_id: 'batch-1', scene_id: 'meeting', uploaded_at: '2026-08-06T10:00:00Z', qa: [],
      evidence: [
        { card_index: 0, segments: [{ segment_id: 'seg_0_1', start_ms: 1000, end_ms: 2400, playback_url: '/api/cards/analysis-meeting/evidence/seg_0_1/audio' }] },
        { card_index: 1, segments: [{ segment_id: 'seg_0_2', start_ms: 3000, end_ms: 4500, playback_url: '/api/cards/analysis-meeting/evidence/seg_0_2/audio' }] },
      ],
      payload: { scene_id: 'meeting', cards: [
        { card: { title: '产品评审', summary: '确定范围' }, detail: { analysis_angle: '范围取舍背后的判断', context_summary: '团队在资源有限的情况下讨论一期范围。', participants: [{ display_name: '林岚', role: '产品负责人' }], key_facts: [{ fact: '一期先做桌面端', interpretation: '团队优先验证高频办公场景' }], quote_analyses: [{ speaker: '林岚', quote: '先把桌面端做透。', context: '讨论多端范围时', surface_meaning: '暂缓移动端', deeper_analysis: '她在用聚焦换取验证速度', interaction_effect: '讨论转向桌面端验收标准' }], arguments: [{ speaker: '林岚', position: '一期只做桌面端', reasoning: '资源不足以同时保证两端质量', supporting_facts: ['桌面端使用频率更高'], assumptions: ['桌面端用户具有代表性'], response_from_others: '研发接受范围缩减', counterpoints: ['移动端需求会被延后'], assessment: '方向合理，但应明确移动端回补条件' }], recommendations: [{ target: '产品负责人', observed_issue: '范围缩减缺少回补条件', evidence_basis: '讨论只确认暂缓移动端', why_it_matters: '可能永久搁置重要需求', recommendation: '定义移动端重启门槛', actions: ['记录触发指标'], suggested_language: '当桌面端周活达到目标后，我们重新评估移动端。', expected_result: '让取舍可逆', caveat: '指标需与业务目标一致' }], sections: [{ section_type: 'tradeoff', title: '被忽略的取舍', narrative: '速度与覆盖面之间的权衡已经发生。', key_points: ['聚焦可加速验证'] }], uncertainties: [{ question: '移动端需求占比是多少？', why_uncertain: '录音未给出数据' }] } },
        { card: { title: '预算复盘', summary: '控制成本' }, detail: { analysis_angle: '成本结构', context_summary: '财务会议讨论外包预算。', participants: [], key_facts: [{ fact: '计划压缩外包', interpretation: null }], quote_analyses: [], arguments: [], recommendations: [], sections: [], uncertainties: [] } },
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
  assert.deepEqual(cards[0].evidence, [{ segmentId: 'seg_0_1', startMs: 1000, endMs: 2400, playbackUrl: '/api/cards/analysis-meeting/evidence/seg_0_1/audio' }])
  assert.deepEqual(cards[1].evidence, [{ segmentId: 'seg_0_2', startMs: 3000, endMs: 4500, playbackUrl: '/api/cards/analysis-meeting/evidence/seg_0_2/audio' }])
  assert.deepEqual(cards[0].detailSections.map((section) => section.kind), ['overview', 'participants', 'facts', 'quotes', 'arguments', 'adaptive', 'recommendations', 'uncertainties'])
  assert.equal(cards[0].detailSections.find((section) => section.kind === 'quotes').entries[0].deeperAnalysis, '她在用聚焦换取验证速度')
  assert.equal(cards[0].detailSections.find((section) => section.kind === 'recommendations').entries[0].suggestedLanguage.includes('周活'), true)
  assert.equal(JSON.stringify(cards[0]).includes('evidence_segment_ids'), false)
  const contentCard = cards.find((card) => card.sceneId === 'content')
  assert.equal(contentCard.details.consumedItems.length, 2)
  assert.equal(contentCard.detailSections.filter((section) => section.eventTitle).length, 2)
  assert.equal(JSON.stringify(contentCard).includes('inferred_title_hint'), false)
  assert.equal(JSON.stringify(contentCard).includes('hidden profile signal'), false)
  assert.equal(JSON.stringify(contentCard).includes('confidence'), false)
  assert.equal(JSON.stringify(contentCard).includes('evidence_segment_ids'), false)
  assert.equal(JSON.stringify(contentCard).includes('generation_reason'), false)
})

test('autonomous cards preserve free sections, quote analysis, and recommendations', () => {
  const state = normalizeFeed({ days: [{ date: '2026-08-11', cards: [{
    id: 'analysis-1', batch_id: 'batch-1', scene_id: 'analysis', uploaded_at: '2026-08-11T10:00:00Z', qa: [], evidence: [],
    payload: { scene_id: 'analysis', cards: [{
      title: '目标与资源不匹配正在消耗投入', summary: '跨片段分析',
      content: [{ type: 'causal_pattern', title: '危险循环', body: '不认同方向会降低投入。', items: ['先书面确认方向'] }],
      quotes: [{ quote: '我不知道最终要到哪里。', context: '讨论目标时', analysis: '指向目标缺失。' }],
      recommendations: [{ title: '建立最小闭环', reason: '保护交付质量', actions: ['写清约束'], suggested_language: '请确认方向。', success_signal: '得到书面确认', caveat: '不含机密' }],
    }] },
  }] }] })
  const card = state.feed[0].cards[0]
  assert.equal(card.label, 'AI 深度分析')
  assert.deepEqual(card.detailSections.map((item) => item.kind), ['analysis', 'autonomous-quotes', 'autonomous-recommendations'])
  assert.equal(card.detailSections[1].entries[0].analysis, '指向目标缺失。')
})


test('imported event and insight cards expose native labels and finding metadata', () => {
  const metadata = JSON.stringify({ card_kind: 'event', scene_types: ['meeting', 'work_conversation'] })
  const state = normalizeFeed({ days: [{ date: '2026-08-11', cards: [{
    id: 'analysis-imported', batch_id: 'batch-1', scene_id: 'analysis', uploaded_at: '2026-08-11T10:00:00Z', qa: [], evidence: [],
    payload: { scene_id: 'analysis', cards: [{
      title: '午餐深谈', summary: '系统性问题正在影响投入。',
      content: [
        { type: 'external_meta', title: '分析类型', body: metadata, items: [] },
        { type: 'finding:fact:high', title: '关键发现', body: '目标没有明确。', items: [] },
        { type: 'scene_reconstruction', title: '场景还原与核心观点', body: '午间交流逐渐从资源问题转向组织机制。\n\n- 目标持续摇摆\n- 关键资源需要等待', items: [] },
        { type: 'analysis', title: '问题如何形成', body: '**危险循环**\n\n1. 不认同方向\n2. 说服失败\n3. 降低投入\n\n不认同方向 → 说服失败 → 降低投入 → 结果变差\n\n**三个并列风险**\n\n1. 行业脱节：需要补课\n2. 逃离投射：可能美化机会\n3. 经验错配：方法不能直接复用\n\n**双方真正关心的事**\n\n| 维度 | 其中一方 | 另一方 |\n| --- | --- | --- |\n| 核心诉求 | 产品目标清晰 | 尽快推进 |', items: [] },
      ],
      quotes: [{ quote: '目标一直没有明确下来', context: '', analysis: '这是直接证据。' }],
      recommendations: [{ title: '先确认目标', reason: '保护执行质量', actions: ['写出成功标准'], suggested_language: null, success_signal: null, caveat: null }],
    }] },
  }] }] })

  const card = state.feed[0].cards[0]
  assert.equal(card.label, '事件分析')
  assert.equal(card.title, '午餐深谈')
  assert.equal(card.summary, '系统性问题正在影响投入。')
  assert.equal(card.showEvidencePlayback, false)
  assert.equal(card.cardKind, 'event')
  assert.deepEqual(card.sceneTypes, ['meeting', 'work_conversation'])
  assert.deepEqual(card.detailSections.map((item) => item.kind), ['autonomous-finding', 'analysis', 'analysis', 'autonomous-quotes', 'autonomous-recommendations'])
  assert.equal(card.detailSections[0].findingType, 'fact')
  assert.equal(card.detailSections[0].confidence, 'high')
  assert.equal(card.detailSections[0].content, '目标没有明确。')
  assert.deepEqual(card.detailSections[1].blocks.map((block) => block.kind), ['paragraph', 'bullet-list'])
  assert.deepEqual(card.detailSections[2].blocks.map((block) => block.kind), ['heading', 'timeline', 'cause-chain', 'heading', 'numbered-list', 'heading', 'matrix'])
  assert.deepEqual(card.detailSections[2].blocks[1].items, ['不认同方向', '说服失败', '降低投入'])
  assert.deepEqual(card.detailSections[2].blocks[2].items, ['不认同方向', '说服失败', '降低投入', '结果变差'])
  assert.deepEqual(card.detailSections[2].blocks[4].items, ['行业脱节：需要补课', '逃离投射：可能美化机会', '经验错配：方法不能直接复用'])
  assert.deepEqual(card.detailSections[2].blocks[6].rows, [
    ['维度', '其中一方', '另一方'],
    ['核心诉求', '产品目标清晰', '尽快推进'],
  ])
})

test('all analysis cards hide evidence playback without autonomous metadata', () => {
  const state = normalizeFeed({ days: [{ date: '2026-08-11', cards: [{
    id: 'analysis-without-meta',
    batch_id: 'batch',
    scene_id: 'analysis',
    uploaded_at: '2026-08-11T10:00:00+08:00',
    qa: [],
    payload: { scene_id: 'analysis', cards: [{ title: '分析', summary: '结论', content: [], quotes: [], recommendations: [] }] },
    evidence: [{ card_index: 0, segments: [{ segment_id: 'seg-1', start_ms: 0, end_ms: 1000, playback_url: '/audio' }] }],
  }] }] })
  const card = state.feed[0].cards[0]

  assert.equal(card.sceneId, 'analysis')
  assert.equal(card.showEvidencePlayback, false)
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
