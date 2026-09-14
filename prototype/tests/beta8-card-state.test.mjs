import assert from 'node:assert/strict'
import test from 'node:test'

import {
  analysisBlocks,
  markdownPresentation,
  normalizeFeed,
  normalizeSectionTitle,
  runtimeMetricsPresentation,
  tokenUsagePresentation,
} from '../src/api/state.js'


const sceneLabels = {
  work_communication: '工作与沟通',
  parenting_family: '亲子与家庭',
  health_state: '健康状态',
  content_consumption: '内容消费',
  inspiration_insight: '灵感与洞察',
  self_growth: '自我成长',
  life_decisions: '生活决策',
}


function row(sceneId, index = 0) {
  return {
    id: `row-${index}`, batch_id: 'batch-1', scene_id: sceneId,
    uploaded_at: '2026-09-02T10:00:00Z',
    payload: {
      scene_id: sceneId,
      cards: [{
        title: `${sceneId} 标题`, summary: `${sceneId} 摘要`,
        evidence_segment_ids: ['seg-1'], external_source_ids: ['source-1'],
      }],
      reportMarkdown: `# ${sceneId} 标题\n\n${sceneId} 摘要\n\n## 正文\n完整内容。`,
    },
    sources: [{ source_id: 'source-1', title: 'Official', url: 'https://example.com/doc' }],
  }
}

function feed(items) {
  return normalizeFeed({ days: [{ date: '2026-09-02', cards: items }] })
}


test('normalizes one Beta 8 markdown card per payload row', () => {
  const normalized = feed([row('work_communication')])
  const cards = normalized.feed.flatMap((batch) => batch.cards)
  assert.equal(cards.length, 1)
  assert.equal(cards[0].title, 'work_communication 标题')
  assert.equal(cards[0].summary, 'work_communication 摘要')
})


test('maps all seven scene ids to user-visible Chinese labels', () => {
  const normalized = feed(Object.keys(sceneLabels).map((sceneId, index) => row(sceneId, index)))
  const cards = normalized.feed.flatMap((batch) => batch.cards)
  assert.deepEqual(Object.fromEntries(cards.map((card) => [card.sceneId, card.label])), sceneLabels)
})


test('preserves reportMarkdown and external sources unchanged', () => {
  const input = row('health_state')
  input.payload.reportMarkdown += '\n\n---\n\n<!-- audio-memory-report-metrics -->\n> 本次报告：12 字'
  const normalized = feed([input])
  const card = normalized.feed[0].cards[0]
  assert.equal(card.reportMarkdown, input.payload.reportMarkdown)
  assert.deepEqual(card.sources, [{ title: 'Official', url: 'https://example.com/doc', domain: 'example.com' }])
})


test('derives the first model H1 for the card header without leaving it in the rendered body', () => {
  const presentation = markdownPresentation('# 标题\n\n核心\n\n## 结论\n\n正文')
  const blocks = analysisBlocks(presentation.body)

  assert.equal(presentation.title, '标题')
  assert.equal(presentation.body.includes('# 标题'), false)
  assert.equal(blocks.some((block) => block.kind === 'heading' && block.level === 1), false)
  assert.deepEqual(blocks, [
    { kind: 'paragraph', text: '核心' },
    { kind: 'heading', level: 2, text: '结论' },
    { kind: 'paragraph', text: '正文' },
  ])
})


test('normalizes historical manual ordinals in H2 through H4 without altering decimals or paragraph text', () => {
  assert.equal(normalizeSectionTitle('一、议题'), '议题')
  assert.equal(normalizeSectionTitle('（一）背景'), '背景')
  assert.equal(normalizeSectionTitle('1.5 倍提升'), '1.5 倍提升')
  assert.equal(normalizeSectionTitle('2.0 模型'), '2.0 模型')
  assert.equal(normalizeSectionTitle('2026.09 计划'), '2026.09 计划')
  assert.deepEqual(
    analysisBlocks('## 一、议题\n\n### (1) 背景\n\n#### 2、细节\n\n正文保留 1.5 倍提升。'),
    [
      { kind: 'heading', level: 2, text: '议题' },
      { kind: 'heading', level: 3, text: '背景' },
      { kind: 'heading', level: 4, text: '细节' },
      { kind: 'paragraph', text: '正文保留 1.5 倍提升。' },
    ],
  )
})


test('provides markdown tables to the renderer through an accessible horizontal-scroll wrapper contract', () => {
  const blocks = analysisBlocks('| A | B |\n|---|---|\n| 1 | 2 |')

  assert.deepEqual(blocks, [{
    kind: 'matrix',
    wrapperClass: 'markdown-table-scroll',
    rows: [['A', 'B'], ['1', '2']],
  }])
})


test('groups current normal calls, repairs, search, and historical totals separately', () => {
  const presentation = runtimeMetricsPresentation({
    run_id: 'run-2',
    run_duration_ms: 1_250,
    stage_durations_ms: {
      event_index: 100,
      all_scenes_v1: 700,
      initial_audit: 120,
      editorial_review: 80,
      search: 50,
      revisions: 100,
      final_audit: 100,
    },
    input_tokens: 2_000,
    output_tokens: 400,
    model_call_count: 7,
    new_model_call_count: 5,
    historical_model_call_count: 2,
    search_input_tokens: 300,
    search_output_tokens: 70,
    search_model_response_count: 2,
    web_search_tool_call_count: 1,
    run_search_input_tokens: 300,
    run_search_output_tokens: 70,
    run_search_model_response_count: 2,
    run_web_search_tool_call_count: 1,
    final_audit_score: 100,
    model_calls: [
      { run_id: 'run-1', stage: 'event_index', attempt_kind: 'normal', duration_ms: 90 },
      { run_id: 'run-2', stage: 'event_index', attempt_kind: 'normal', duration_ms: 100 },
      { run_id: 'run-2', stage: 'all_scenes_v1', attempt_kind: 'normal', duration_ms: 650 },
      { run_id: 'run-2', stage: 'all_scenes_v1', attempt_kind: 'schema_repair', duration_ms: 50 },
    ],
  })

  assert.deepEqual(presentation.normal.calls.map((item) => item.stage), [
    'event_index', 'all_scenes_v1',
  ])
  assert.deepEqual(presentation.recovery.calls.map((item) => item.attempt_kind), [
    'schema_repair',
  ])
  assert.deepEqual(presentation.search, {
    durationMs: 50,
    inputTokens: 300,
    outputTokens: 70,
    tokenUsageUnavailableReason: null,
    modelResponses: 2,
    toolCalls: 1,
  })
  assert.equal(presentation.history.modelCalls, 2)
  assert.equal(presentation.summary.finalAuditScore, 100)
})


test('keeps legacy aggregate metrics visible when no run id exists', () => {
  const presentation = runtimeMetricsPresentation({
    run_id: null,
    input_tokens: 1200,
    output_tokens: 300,
    model_call_count: 4,
    run_input_tokens: 0,
    run_output_tokens: 0,
    search_input_tokens: 200,
    search_output_tokens: 50,
    run_search_input_tokens: 0,
    run_search_output_tokens: 0,
    model_calls: [{ stage: 'writing', duration_ms: 100 }],
  })

  assert.equal(presentation.summary.inputTokens, 1200)
  assert.equal(presentation.summary.outputTokens, 300)
  assert.equal(presentation.search.inputTokens, 200)
  assert.equal(presentation.search.outputTokens, 50)
  assert.equal(presentation.normal.calls.length, 1)
  assert.equal(presentation.history.modelCalls, 0)
})


test('does not display zero calls when only aggregate retry counts were persisted', () => {
  const presentation = runtimeMetricsPresentation({
    run_id: null,
    model_call_count: 11,
    new_model_call_count: 6,
    model_calls: [],
  })

  assert.equal(presentation.normal.count, 5)
  assert.equal(presentation.recovery.count, 6)
})


test('renders unknown provider token usage as unavailable instead of exact zero', () => {
  const presentation = runtimeMetricsPresentation({
    run_id: 'run-unknown',
    run_input_tokens: null,
    run_output_tokens: null,
    input_tokens: null,
    output_tokens: null,
    token_usage_unavailable_reason: 'provider response omitted usage',
    model_calls: [{
      run_id: 'run-unknown', stage: 'event_index', attempt_kind: 'normal',
      input_tokens: null, output_tokens: null, duration_ms: 10,
    }],
  })

  assert.equal(presentation.summary.inputTokens, null)
  assert.equal(presentation.summary.outputTokens, null)
  assert.equal(
    presentation.summary.tokenUsageUnavailableReason,
    'provider response omitted usage',
  )
})


test('renders unknown native search tokens without hiding observable call counts', () => {
  const presentation = runtimeMetricsPresentation({
    run_id: 'run-search-unknown',
    search_input_tokens: null,
    search_output_tokens: null,
    run_search_input_tokens: null,
    run_search_output_tokens: null,
    search_token_usage_unavailable_reason: 'native search provider response omitted usage',
    search_model_response_count: 2,
    web_search_tool_call_count: 1,
    run_search_model_response_count: 2,
    run_web_search_tool_call_count: 1,
  })

  assert.equal(presentation.search.inputTokens, null)
  assert.equal(presentation.search.outputTokens, null)
  assert.equal(
    presentation.search.tokenUsageUnavailableReason,
    'native search provider response omitted usage',
  )
  assert.equal(presentation.search.modelResponses, 2)
  assert.equal(presentation.search.toolCalls, 1)
  assert.equal(
    tokenUsagePresentation(
      presentation.search.inputTokens,
      presentation.search.outputTokens,
      presentation.search.tokenUsageUnavailableReason,
    ),
    '不可得（native search provider response omitted usage）',
  )
})
