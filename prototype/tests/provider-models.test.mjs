import assert from 'node:assert/strict'
import test from 'node:test'

import { configurableProviderEntries, normalizeProviders } from '../src/api/state.js'


test('provider response exposes GLM and curated model choices to the UI', () => {
  const normalized = normalizeProviders({ providers: [{
    provider_id: 'glm',
    display_name: 'GLM',
    model_id: 'glm-5.2',
    model_options: [
      { model_id: 'glm-5.2', label: '最高质量' },
      { model_id: 'glm-4.7-flash', label: '最高性价比' },
    ],
    state: 'unconfigured',
    active: false,
  }] })

  assert.deepEqual(normalized.providers.glm.models, [
    { id: 'glm-5.2', label: '最高质量' },
    { id: 'glm-4.7-flash', label: '最高性价比' },
  ])
  assert.equal(normalized.providers.glm.modelName, 'glm-5.2')
  assert.deepEqual(
    configurableProviderEntries(normalized.providers).map(([id]) => id),
    ['kimi', 'deepseek', 'openai'],
  )
})
