import assert from 'node:assert/strict'
import test from 'node:test'

import { runtimeEnvironment } from '../src/api/client.js'

test('development UI marks a development backend', () => {
  assert.deepEqual(runtimeEnvironment('development', { profile: 'development' }), {
    profile: 'development', blocked: false, label: '开发环境', message: '', version: '',
  })
})

test('integration acceptance UI uses the backend supplied version label', () => {
  assert.deepEqual(runtimeEnvironment('development', {
    profile: 'development',
    environment_label: 'v0.1.0-beta.3 集成验收',
  }), {
    profile: 'development', blocked: false,
    label: 'v0.1.0-beta.3 集成验收', message: '', version: '',
  })
})

test('development UI blocks a production backend', () => {
  const state = runtimeEnvironment('development', { profile: 'production' })

  assert.equal(state.blocked, true)
  assert.match(state.message, /正式环境/)
})

test('production UI has no environment label', () => {
  assert.deepEqual(runtimeEnvironment('', { profile: 'production' }), {
    profile: 'production', blocked: false, label: '', message: '', version: '',
  })
})

test('an expected profile blocks a missing or invalid health profile', () => {
  for (const payload of [null, {}, { profile: 'preview' }]) {
    const state = runtimeEnvironment('development', payload)
    assert.equal(state.blocked, true)
    assert.match(state.message, /无法确认/)
  }
})
