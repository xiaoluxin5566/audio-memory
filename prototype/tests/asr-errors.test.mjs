import assert from 'node:assert/strict'
import test from 'node:test'

import { asrErrorMessage } from '../src/asrErrors.js'

test('permission denied explains every provider-side cause observed in real validation', () => {
  const message = asrErrorMessage(
    { code: 'permission_denied', message: '{"detail":{"code":"permission_denied"}}' },
    '连接不可用',
  )

  assert.match(message, /开通/)
  assert.match(message, /资源包/)
  assert.match(message, /欠费/)
  assert.doesNotMatch(message, /"detail"/)
})

test('unknown ASR errors preserve an actionable server message or local fallback', () => {
  assert.equal(asrErrorMessage({ code: 'other', message: '具体错误' }, '默认'), '具体错误')
  assert.equal(asrErrorMessage({ code: 'other' }, '默认'), '默认')
})
