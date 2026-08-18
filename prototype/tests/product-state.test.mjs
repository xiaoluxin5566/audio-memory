import test from 'node:test'
import assert from 'node:assert/strict'

import { createInitialState, formatJobEta, getFeedbackFormState, jobFailureCopy, jobProgressValue, orderCards } from '../src/store.js'


test('initial state waits for the runtime autonomous prompt list', () => {
  const state = createInitialState()
  assert.deepEqual(Object.keys(state.prompts), [])
  assert.deepEqual(state.feed, [])
  assert.deepEqual(state.history, [])
})


test('cards follow approved batch order', () => {
  const cards = ['inspiration', 'meeting', 'growth', 'parenting', 'content']
    .map((sceneId) => ({ sceneId }))
  assert.deepEqual(orderCards(cards).map((item) => item.sceneId), [
    'meeting', 'parenting', 'content', 'growth', 'inspiration',
  ])
})


test('feedback details are required only for inaccurate ratings', () => {
  assert.deepEqual(getFeedbackFormState(''), { showDetails: false, canSubmit: false })
  assert.deepEqual(getFeedbackFormState('accurate'), { showDetails: false, canSubmit: true })
  assert.deepEqual(getFeedbackFormState('inaccurate', '   '), { showDetails: true, canSubmit: false })
  assert.deepEqual(getFeedbackFormState('inaccurate', '会议结论遗漏了预算限制'), {
    showDetails: true,
    canSubmit: true,
  })
})


test('job ETA copy follows transcription and analysis states', () => {
  assert.equal(formatJobEta({ stage: 'transcribing', eta_state: 'estimating' }), '正在估算剩余时间…')
  assert.equal(formatJobEta({ stage: 'transcribing', eta_state: 'ready', eta_seconds: 59 }), '预计不到 1 分钟')
  assert.equal(formatJobEta({ stage: 'transcribing', eta_state: 'ready', eta_seconds: 901 }), '预计还需约 16 分钟')
  assert.equal(formatJobEta({ stage: 'analyzing' }), '正在生成分析结果…')
})


test('report audit failure is presented as generated and retryable', () => {
  assert.deepEqual(jobFailureCopy({ error_code: 'report_audit_pending' }), {
    title: '报告已生成，审计待重试',
    body: '已保留报告初稿和已完成的审计块，重试不会重新转写。',
    action: '继续审计',
  })
})

test('job progress prefers bounded live progress and preserves durable fallback', () => {
  assert.equal(jobProgressValue({ progress_percent: 63 }), 63)
  assert.equal(jobProgressValue({ progress_percent: 63, live_progress_percent: 66.375 }), 66.375)
  assert.equal(jobProgressValue({ progress_percent: 63, live_progress_percent: 120 }), 100)
})
