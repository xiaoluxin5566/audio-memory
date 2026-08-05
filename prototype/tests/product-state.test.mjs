import test from 'node:test'
import assert from 'node:assert/strict'

import { createInitialState, getFeedbackFormState, orderCards } from '../src/store.js'


test('initial state has six fixed prompt scenes and no persisted content', () => {
  const state = createInitialState()
  assert.deepEqual(Object.keys(state.prompts), [
    'todo', 'meeting', 'parenting', 'content', 'growth', 'inspiration',
  ])
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
