import assert from 'node:assert/strict'
import test from 'node:test'

import { configurableProviderEntries, normalizeProviders } from '../src/api/state.js'


test('configuration UI exposes only DeepSeek V4 Pro', () => {
  const normalized = normalizeProviders({ providers: [{
    provider_id: 'deepseek',
    display_name: 'DeepSeek',
    model_id: 'deepseek-v4-pro',
    model_options: [
      { model_id: 'deepseek-v4-pro', label: '最高质量' },
    ],
    state: 'unconfigured',
    active: false,
  }] })

  assert.deepEqual(normalized.providers.deepseek.models, [
    { id: 'deepseek-v4-pro', label: '最高质量' },
  ])
  assert.equal(normalized.providers.deepseek.modelName, 'deepseek-v4-pro')
  assert.deepEqual(Object.keys(normalized.providers), ['kimi', 'deepseek', 'openai', 'glm'])
  assert.deepEqual(
    configurableProviderEntries(normalized.providers).map(([id]) => id),
    ['deepseek'],
  )
})
