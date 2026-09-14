import assert from 'node:assert/strict'
import test from 'node:test'

import { configurableProviderEntries, normalizeProviders } from '../src/api/state.js'


test('configuration UI exposes DeepSeek report analysis and Kimi search', () => {
  const normalized = normalizeProviders({ providers: [{
    provider_id: 'deepseek',
    display_name: 'DeepSeek',
    model_id: 'deepseek-v4-pro',
    model_options: [
      { model_id: 'deepseek-v4-pro', label: '最高质量' },
    ],
    state: 'unconfigured',
    active: false,
  }, {
    provider_id: 'kimi', display_name: 'Kimi', model_id: 'kimi-k2.6',
    model_options: [{ model_id: 'kimi-k2.6', label: 'K2.6' }],
    state: 'unconfigured', active: false,
  }] })

  assert.deepEqual(normalized.providers.deepseek.models, [
    { id: 'deepseek-v4-pro', label: '最高质量' },
  ])
  assert.equal(normalized.providers.deepseek.modelName, 'deepseek-v4-pro')
  assert.deepEqual(Object.keys(normalized.providers), ['kimi', 'deepseek', 'openai', 'glm'])
  assert.deepEqual(
    configurableProviderEntries(normalized.providers).map(([id]) => id),
    ['deepseek', 'kimi'],
  )
})
