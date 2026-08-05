import assert from 'node:assert/strict'
import test from 'node:test'

import { apiRequest } from '../src/api/client.js'


test('plain-text server failures become readable request errors', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response('Internal Server Error', {
    status: 500,
    headers: { 'Content-Type': 'text/plain' },
  })
  try {
    await assert.rejects(apiRequest('/providers/deepseek/key'), {
      message: '本地服务内部错误，请重试或运行诊断',
      status: 500,
    })
  } finally {
    globalThis.fetch = originalFetch
  }
})


test('provider key field is intentionally visible during entry', async () => {
  const source = await import('node:fs/promises').then((fs) => fs.readFile(new URL('../src/App.jsx', import.meta.url), 'utf8'))
  assert.match(source, /<input type="text" value=\{key\}/)
})


test('successful provider configuration activates it and closes the modal', async () => {
  const source = await import('node:fs/promises').then((fs) => fs.readFile(new URL('../src/App.jsx', import.meta.url), 'utf8'))
  const submitBody = source.slice(source.indexOf('async function submit()'), source.indexOf('async function revalidate()'))
  assert.match(submitBody, /await api\.saveProviderKey/)
  assert.match(submitBody, /await api\.activateProvider\(providerId\)/)
  assert.match(submitBody, /onClose\(\)/)
  assert.ok(submitBody.indexOf('activateProvider') < submitBody.indexOf('onClose()'))
})
