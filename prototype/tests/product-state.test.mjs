import test from 'node:test'
import assert from 'node:assert/strict'

import { createInitialState, formatJobEta, getFeedbackFormState, orderCards } from '../src/store.js'


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
