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

test('report preview does not wait for the live history API before rendering', async () => {
  const source = await import('node:fs/promises').then((fs) => fs.readFile(new URL('../src/api/client.js', import.meta.url), 'utf8'))
  const api = source.slice(source.indexOf('export const api'), source.length)

  assert.match(api, /history:\s*\(\)\s*=>\s*isReportPreview\(\)\s*\?\s*Promise\.resolve\(\{\s*days:\s*\[\]\s*\}\)/)
})


test('mutating requests use one memory-only local session and an action idempotency key', async () => {
  const client = await import(`../src/api/client.js?session-headers=${Date.now()}`)
  assert.equal(typeof client.getLocalSessionHeaders, 'function')

  const originalFetch = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options })
    if (url === '/api/session') {
      return Response.json({ token: 'page-session-token' })
    }
    return Response.json({ ok: true }, { status: 201 })
  }
  try {
    await client.apiRequest('/jobs', {
      method: 'POST',
      idempotencyKey: 'same-action-key',
    })
    await client.apiRequest('/jobs', {
      method: 'POST',
      idempotencyKey: 'same-action-key',
    })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(requests.filter(({ url }) => url === '/api/session').length, 1)
  for (const request of requests.filter(({ url }) => url === '/api/jobs')) {
    assert.equal(request.options.headers['X-Audio-Memory-Session'], 'page-session-token')
    assert.equal(request.options.headers['Idempotency-Key'], 'same-action-key')
    assert.equal('idempotencyKey' in request.options, false)
  }
})


test('read-only calls skip sessions while separate actions receive separate UUID keys', async () => {
  const client = await import(`../src/api/client.js?action-keys=${Date.now()}`)
  const originalFetch = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options })
    if (url === '/api/session') return Response.json({ token: 'memory-token' })
    return Response.json({ ok: true })
  }
  try {
    await client.apiRequest('/providers')
    await client.apiRequest('/jobs', { method: 'POST' })
    await client.apiRequest('/jobs', { method: 'POST' })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(requests[0].url, '/api/providers')
  assert.equal(requests[0].options.headers['X-Audio-Memory-Session'], undefined)
  const mutations = requests.filter(({ url }) => url === '/api/jobs')
  assert.match(mutations[0].options.headers['Idempotency-Key'], /^[0-9a-f-]{36}$/)
  assert.notEqual(
    mutations[0].options.headers['Idempotency-Key'],
    mutations[1].options.headers['Idempotency-Key'],
  )
})


test('an expired fetch session refreshes once and retries the same action key', async () => {
  const client = await import(`../src/api/client.js?expired-fetch=${Date.now()}`)
  const originalFetch = globalThis.fetch
  const sessions = ['expired-token', 'fresh-token']
  const actions = []
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/session') return Response.json({ token: sessions.shift() })
    actions.push(options.headers)
    if (actions.length === 1) {
      return Response.json(
        { detail: { code: 'invalid_session', message: 'expired' } },
        { status: 401 },
      )
    }
    return Response.json({ ok: true })
  }
  try {
    await client.apiRequest('/jobs', {
      method: 'POST',
      idempotencyKey: 'refresh-same-action',
    })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(actions.length, 2)
  assert.equal(actions[0]['X-Audio-Memory-Session'], 'expired-token')
  assert.equal(actions[1]['X-Audio-Memory-Session'], 'fresh-token')
  assert.equal(actions[0]['Idempotency-Key'], 'refresh-same-action')
  assert.equal(actions[1]['Idempotency-Key'], 'refresh-same-action')
})


test('response-lost fetch retries once with the original action key', async () => {
  const client = await import(`../src/api/client.js?transport-fetch=${Date.now()}`)
  const originalFetch = globalThis.fetch
  const actions = []
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/session') return Response.json({ token: 'transport-token' })
    actions.push(options.headers)
    if (actions.length === 1) throw new TypeError('response lost after commit')
    return Response.json({ ok: true }, { status: 201 })
  }
  try {
    await client.apiRequest('/jobs', { method: 'POST' })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(actions.length, 2)
  assert.match(actions[0]['Idempotency-Key'], /^[0-9a-f-]{36}$/)
  assert.equal(actions[0]['Idempotency-Key'], actions[1]['Idempotency-Key'])
  assert.equal(actions[0]['X-Audio-Memory-Session'], actions[1]['X-Audio-Memory-Session'])
})


test('response lost after fetch session refresh retries the refreshed action key', async () => {
  const client = await import(`../src/api/client.js?refresh-transport-fetch=${Date.now()}`)
  const originalFetch = globalThis.fetch
  const sessions = ['expired-token', 'fresh-token']
  const actions = []
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/session') return Response.json({ token: sessions.shift() })
    actions.push(options.headers)
    if (actions.length === 1) {
      return Response.json(
        { detail: { code: 'invalid_session', message: 'expired' } },
        { status: 401 },
      )
    }
    if (actions.length === 2) throw new TypeError('refreshed response lost after commit')
    return Response.json({ replayed: true }, { status: 201 })
  }
  try {
    const result = await client.apiRequest('/jobs', {
      method: 'POST',
      idempotencyKey: 'refresh-then-transport',
    })
    assert.deepEqual(result, { replayed: true })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(actions.length, 3)
  assert.deepEqual(
    actions.map((headers) => headers['X-Audio-Memory-Session']),
    ['expired-token', 'fresh-token', 'fresh-token'],
  )
  assert.deepEqual(
    actions.map((headers) => headers['Idempotency-Key']),
    ['refresh-then-transport', 'refresh-then-transport', 'refresh-then-transport'],
  )
})


test('body stream lost after fetch session refresh retries the refreshed action key', async () => {
  const client = await import(`../src/api/client.js?refresh-body-stream=${Date.now()}`)
  const originalFetch = globalThis.fetch
  const sessions = ['expired-token', 'fresh-token']
  const actions = []
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/session') return Response.json({ token: sessions.shift() })
    actions.push(options.headers)
    if (actions.length === 1) {
      return Response.json({ detail: { code: 'invalid_session' } }, { status: 401 })
    }
    if (actions.length === 2) {
      return new Response(new ReadableStream({
        start(controller) {
          controller.error(new TypeError('body stream lost after commit'))
        },
      }), { status: 201 })
    }
    return Response.json({ replayed: true }, { status: 201 })
  }
  try {
    const result = await client.apiRequest('/jobs', {
      method: 'POST',
      idempotencyKey: 'refresh-then-body-loss',
    })
    assert.deepEqual(result, { replayed: true })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(actions.length, 3)
  assert.deepEqual(
    actions.map((headers) => headers['X-Audio-Memory-Session']),
    ['expired-token', 'fresh-token', 'fresh-token'],
  )
  assert.ok(actions.every((headers) => (
    headers['Idempotency-Key'] === 'refresh-then-body-loss'
  )))
})


test('fetch retry state machine bounds refresh and transport retries independently', async () => {
  const client = await import(`../src/api/client.js?bounded-fetch=${Date.now()}`)
  const originalFetch = globalThis.fetch
  const sessions = ['expired-token', 'fresh-token']
  const actions = []
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/session') return Response.json({ token: sessions.shift() })
    actions.push(options.headers)
    if (actions.length === 1) {
      return Response.json({ detail: { code: 'invalid_session' } }, { status: 401 })
    }
    throw new TypeError(`lost response ${actions.length}`)
  }
  try {
    await assert.rejects(
      client.apiRequest('/jobs', {
        method: 'POST',
        idempotencyKey: 'bounded-composed-retries',
      }),
      { message: 'lost response 3' },
    )
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(actions.length, 3)
  assert.equal(actions[1]['X-Audio-Memory-Session'], 'fresh-token')
  assert.equal(actions[2]['X-Audio-Memory-Session'], 'fresh-token')
  assert.ok(actions.every((headers) => (
    headers['Idempotency-Key'] === 'bounded-composed-retries'
  )))
})


test('fetch transport retry can be followed by one session refresh', async () => {
  const client = await import(`../src/api/client.js?transport-then-refresh=${Date.now()}`)
  const originalFetch = globalThis.fetch
  const sessions = ['initial-token', 'refreshed-token']
  const actions = []
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/session') return Response.json({ token: sessions.shift() })
    actions.push(options.headers)
    if (actions.length === 1) throw new TypeError('first response lost')
    if (actions.length === 2) {
      return Response.json({ detail: { code: 'invalid_session' } }, { status: 401 })
    }
    return Response.json({ replayed: true }, { status: 201 })
  }
  try {
    const result = await client.apiRequest('/jobs', {
      method: 'POST',
      idempotencyKey: 'transport-then-refresh',
    })
    assert.deepEqual(result, { replayed: true })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.equal(actions.length, 3)
  assert.deepEqual(
    actions.map((headers) => headers['X-Audio-Memory-Session']),
    ['initial-token', 'initial-token', 'refreshed-token'],
  )
  assert.ok(actions.every((headers) => (
    headers['Idempotency-Key'] === 'transport-then-refresh'
  )))
})


test('expired XHR upload session refreshes and retries the explicit action key', async () => {
  const originalFetch = globalThis.fetch
  const OriginalXHR = globalThis.XMLHttpRequest
  const requests = []
  const sessionRequests = []

  class FakeXHR {
    constructor() {
      this.headers = {}
      this.listeners = {}
      this.upload = { addEventListener() {} }
      requests.push(this)
    }

    open(method, url) {
      this.method = method
      this.url = url
    }

    setRequestHeader(name, value) {
      this.headers[name] = value
    }

    addEventListener(name, listener) {
      this.listeners[name] = listener
    }

    send(body) {
      this.body = body
      if (requests.length === 1) {
        this.status = 401
        this.response = { detail: { code: 'invalid_session', message: 'expired' } }
      } else {
        this.status = 201
        this.response = { id: 'file-1' }
      }
      queueMicrotask(() => this.listeners.load())
    }
  }

  globalThis.fetch = async (url) => {
    sessionRequests.push(url)
    const token = sessionRequests.length === 1 ? 'upload-session-token' : 'fresh-upload-token'
    return Response.json({ token })
  }
  globalThis.XMLHttpRequest = FakeXHR
  try {
    const upload = await import(`../src/api/upload.js?xhr-security=${Date.now()}`)
    const file = new File(['audio'], 'meeting.mp3', { type: 'audio/mpeg' })
    await upload.uploadFile('job/unsafe', file, { idempotencyKey: 'upload-retry-key' })
  } finally {
    globalThis.fetch = originalFetch
    globalThis.XMLHttpRequest = OriginalXHR
  }

  assert.deepEqual(sessionRequests, ['/api/session', '/api/session'])
  assert.equal(requests.length, 2)
  assert.equal(requests[0].url, '/api/jobs/job%2Funsafe/files')
  assert.equal(requests[0].headers['X-Audio-Memory-Session'], 'upload-session-token')
  assert.equal(requests[0].headers['Idempotency-Key'], 'upload-retry-key')
  assert.equal(requests[1].headers['X-Audio-Memory-Session'], 'fresh-upload-token')
  assert.equal(requests[1].headers['Idempotency-Key'], 'upload-retry-key')
})


test('network-timeout XHR upload retries once with the original action key', async () => {
  const originalFetch = globalThis.fetch
  const OriginalXHR = globalThis.XMLHttpRequest
  const requests = []

  class FakeXHR {
    constructor() {
      this.headers = {}
      this.listeners = {}
      this.upload = { addEventListener() {} }
      requests.push(this)
    }

    open(method, url) {
      this.method = method
      this.url = url
    }

    setRequestHeader(name, value) {
      this.headers[name] = value
    }

    addEventListener(name, listener) {
      this.listeners[name] = listener
    }

    send(body) {
      this.body = body
      if (requests.length === 1) {
        queueMicrotask(() => this.listeners.timeout())
      } else {
        this.status = 201
        this.response = { id: 'file-after-replay' }
        queueMicrotask(() => this.listeners.load())
      }
    }
  }

  globalThis.fetch = async () => Response.json({ token: 'xhr-transport-token' })
  globalThis.XMLHttpRequest = FakeXHR
  try {
    const upload = await import(`../src/api/upload.js?xhr-transport=${Date.now()}`)
    const file = new File(['audio'], 'meeting.mp3', { type: 'audio/mpeg' })
    await upload.uploadFile('job-1', file, { idempotencyKey: 'xhr-response-lost' })
  } finally {
    globalThis.fetch = originalFetch
    globalThis.XMLHttpRequest = OriginalXHR
  }

  assert.equal(requests.length, 2)
  assert.equal(requests[0].headers['Idempotency-Key'], 'xhr-response-lost')
  assert.equal(requests[1].headers['Idempotency-Key'], 'xhr-response-lost')
  assert.equal(
    requests[0].headers['X-Audio-Memory-Session'],
    requests[1].headers['X-Audio-Memory-Session'],
  )
})


test('XHR transport retry still applies after refreshing an expired session', async () => {
  const originalFetch = globalThis.fetch
  const OriginalXHR = globalThis.XMLHttpRequest
  const requests = []
  const sessions = ['composed-upload-token-1', 'composed-upload-token-2']

  class FakeXHR {
    constructor() {
      this.headers = {}
      this.listeners = {}
      this.upload = { addEventListener() {} }
      requests.push(this)
    }

    open(method, url) {
      this.method = method
      this.url = url
    }

    setRequestHeader(name, value) {
      this.headers[name] = value
    }

    addEventListener(name, listener) {
      this.listeners[name] = listener
    }

    send(body) {
      this.body = body
      if (requests.length === 1) {
        this.status = 401
        this.response = { detail: { code: 'invalid_session' } }
        queueMicrotask(() => this.listeners.load())
      } else if (requests.length === 2) {
        queueMicrotask(() => this.listeners.timeout())
      } else {
        this.status = 201
        this.response = { id: 'file-replayed-after-refresh' }
        queueMicrotask(() => this.listeners.load())
      }
    }
  }

  globalThis.fetch = async () => Response.json({ token: sessions.shift() })
  globalThis.XMLHttpRequest = FakeXHR
  try {
    const upload = await import(`../src/api/upload.js?xhr-composed=${Date.now()}`)
    const file = new File(['audio'], 'meeting.mp3', { type: 'audio/mpeg' })
    const result = await upload.uploadFile('job-1', file, {
      idempotencyKey: 'xhr-refresh-then-transport',
    })
    assert.deepEqual(result, { id: 'file-replayed-after-refresh' })
  } finally {
    globalThis.fetch = originalFetch
    globalThis.XMLHttpRequest = OriginalXHR
  }

  assert.equal(requests.length, 3)
  const requestTokens = requests.map(
    (request) => request.headers['X-Audio-Memory-Session'],
  )
  assert.notEqual(requestTokens[0], requestTokens[1])
  assert.equal(requestTokens[1], requestTokens[2])
  assert.ok(requests.every((request) => (
    request.headers['Idempotency-Key'] === 'xhr-refresh-then-transport'
  )))
})


test('XHR retry state machine bounds refresh and transport retries independently', async () => {
  const originalFetch = globalThis.fetch
  const OriginalXHR = globalThis.XMLHttpRequest
  const requests = []
  const sessions = ['bounded-upload-token-1', 'bounded-upload-token-2']

  class FakeXHR {
    constructor() {
      this.headers = {}
      this.listeners = {}
      this.upload = { addEventListener() {} }
      requests.push(this)
    }

    open() {}

    setRequestHeader(name, value) {
      this.headers[name] = value
    }

    addEventListener(name, listener) {
      this.listeners[name] = listener
    }

    send() {
      if (requests.length === 1) {
        this.status = 401
        this.response = { detail: { code: 'invalid_session' } }
        queueMicrotask(() => this.listeners.load())
        return
      }
      queueMicrotask(() => this.listeners.timeout())
    }
  }

  globalThis.fetch = async () => Response.json({ token: sessions.shift() })
  globalThis.XMLHttpRequest = FakeXHR
  try {
    const upload = await import(`../src/api/upload.js?xhr-bounded=${Date.now()}`)
    const file = new File(['audio'], 'meeting.mp3', { type: 'audio/mpeg' })
    await assert.rejects(
      upload.uploadFile('job-1', file, {
        idempotencyKey: 'xhr-bounded-composed-retries',
      }),
      { code: 'network_timeout', message: '上传超时，请重试' },
    )
  } finally {
    globalThis.fetch = originalFetch
    globalThis.XMLHttpRequest = OriginalXHR
  }

  assert.equal(requests.length, 3)
  const requestTokens = requests.map(
    (request) => request.headers['X-Audio-Memory-Session'],
  )
  assert.notEqual(requestTokens[0], requestTokens[1])
  assert.equal(requestTokens[1], requestTokens[2])
  assert.ok(requests.every((request) => (
    request.headers['Idempotency-Key'] === 'xhr-bounded-composed-retries'
  )))
})


test('XHR transport retry can be followed by one session refresh', async () => {
  const originalFetch = globalThis.fetch
  const OriginalXHR = globalThis.XMLHttpRequest
  const requests = []
  const sessions = ['xhr-order-token-1', 'xhr-order-token-2']

  class FakeXHR {
    constructor() {
      this.headers = {}
      this.listeners = {}
      this.upload = { addEventListener() {} }
      requests.push(this)
    }

    open() {}

    setRequestHeader(name, value) {
      this.headers[name] = value
    }

    addEventListener(name, listener) {
      this.listeners[name] = listener
    }

    send() {
      if (requests.length === 1) {
        queueMicrotask(() => this.listeners.timeout())
      } else if (requests.length === 2) {
        this.status = 401
        this.response = { detail: { code: 'invalid_session' } }
        queueMicrotask(() => this.listeners.load())
      } else {
        this.status = 201
        this.response = { id: 'file-after-transport-and-refresh' }
        queueMicrotask(() => this.listeners.load())
      }
    }
  }

  globalThis.fetch = async () => Response.json({ token: sessions.shift() })
  globalThis.XMLHttpRequest = FakeXHR
  try {
    const upload = await import(`../src/api/upload.js?xhr-order=${Date.now()}`)
    const file = new File(['audio'], 'meeting.mp3', { type: 'audio/mpeg' })
    const result = await upload.uploadFile('job-1', file, {
      idempotencyKey: 'xhr-transport-then-refresh',
    })
    assert.deepEqual(result, { id: 'file-after-transport-and-refresh' })
  } finally {
    globalThis.fetch = originalFetch
    globalThis.XMLHttpRequest = OriginalXHR
  }

  assert.equal(requests.length, 3)
  const requestTokens = requests.map(
    (request) => request.headers['X-Audio-Memory-Session'],
  )
  assert.equal(requestTokens[0], requestTokens[1])
  assert.notEqual(requestTokens[1], requestTokens[2])
  assert.ok(requests.every((request) => (
    request.headers['Idempotency-Key'] === 'xhr-transport-then-refresh'
  )))
})
