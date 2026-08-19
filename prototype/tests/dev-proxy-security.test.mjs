import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { createServer as createHttpServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { once } from 'node:events'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer as createViteServer } from 'vite'


test('development Vite defaults to the isolated backend and declares its expected profile', async () => {
  const source = await readFile(new URL('../vite.config.mjs', import.meta.url), 'utf8')

  assert.match(source, /http:\/\/127\.0\.0\.1:8766/)
  assert.match(source, /VITE_AUDIO_MEMORY_EXPECTED_PROFILE/)
  assert.match(source, /development/)
})


async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  return server.address().port
}


async function close(server) {
  await new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve())
  })
}


async function waitForBackend(url, backend) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (backend.exitCode !== null) throw new Error(`backend exited with ${backend.exitCode}`)
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {
      // The loopback listener is not ready yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  throw new Error('test backend did not become ready')
}


test('loopback Vite proxy preserves the backend same-origin mutation boundary', { concurrency: false }, async () => {
  const reservedBackendPortServer = createHttpServer()
  const backendPort = await listen(reservedBackendPortServer)
  await close(reservedBackendPortServer)
  const backendOrigin = `http://127.0.0.1:${backendPort}`
  const testRoot = await mkdtemp(join(tmpdir(), 'audio-memory-dev-proxy-'))
  const backendRoot = fileURLToPath(new URL('../../backend/', import.meta.url))
  const backend = spawn(
    fileURLToPath(new URL('../../backend/.venv/bin/python', import.meta.url)),
    [
      '-m', 'uvicorn', 'dev_proxy_app:app',
      '--app-dir', join(backendRoot, 'tests/support'),
      '--host', '127.0.0.1',
      '--port', String(backendPort),
      '--log-level', 'error',
    ],
    {
      cwd: backendRoot,
      env: {
        ...process.env,
        AUDIO_MEMORY_TEST_SECURITY_DB: join(testRoot, 'security.sqlite3'),
        AUDIO_MEMORY_TEST_BACKEND_PORT: String(backendPort),
        AUDIO_MEMORY_TEST_BACKEND_PROFILE: 'development',
      },
      stdio: 'ignore',
    },
  )
  await waitForBackend(`${backendOrigin}/api/count`, backend)

  const reservedDevPortServer = createHttpServer()
  const devPort = await listen(reservedDevPortServer)
  await close(reservedDevPortServer)
  const previousBackend = process.env.AUDIO_MEMORY_BACKEND_URL
  const previousDevPort = process.env.AUDIO_MEMORY_DEV_PORT
  process.env.AUDIO_MEMORY_BACKEND_URL = backendOrigin
  process.env.AUDIO_MEMORY_DEV_PORT = String(devPort)

  let vite
  try {
    vite = await createViteServer({
      configFile: fileURLToPath(new URL('../vite.config.mjs', import.meta.url)),
      logLevel: 'silent',
    })
    await vite.listen()
    const devOrigin = `http://127.0.0.1:${devPort}`
    const health = await fetch(`${devOrigin}/api/health`)
    const crossSiteSession = await fetch(`${devOrigin}/api/session`, {
      headers: { Origin: 'https://evil.example' },
    })
    const crossSiteMetadataSession = await fetch(`${devOrigin}/api/session`, {
      headers: { 'Sec-Fetch-Site': 'cross-site' },
    })
    const session = await fetch(`${devOrigin}/api/session`)
    const token = (await session.json()).token
    const mutation = await fetch(`${devOrigin}/api/effect`, {
      method: 'POST',
      headers: {
        Origin: devOrigin,
        'X-Audio-Memory-Session': token,
        'Idempotency-Key': 'dev-action',
      },
      body: '{}',
    })
    const crossSite = await fetch(`${devOrigin}/api/effect`, {
      method: 'POST',
      headers: {
        Origin: 'https://evil.example',
        'X-Audio-Memory-Session': token,
        'Idempotency-Key': 'evil-action',
      },
      body: '{}',
    })
    const missingOrigin = await fetch(`${devOrigin}/api/effect`, {
      method: 'POST',
      headers: {
        'X-Audio-Memory-Session': token,
        'Idempotency-Key': 'missing-origin',
      },
      body: '{}',
    })
    const reportPreview = await fetch(`${devOrigin}/output/deepseek-historical-report-preview.json`)

    assert.equal(health.status, 200)
    assert.deepEqual(await health.json(), { status: 'ok', profile: 'development' })
    assert.equal(crossSiteSession.status, 403)
    assert.equal(crossSiteMetadataSession.status, 403)
    assert.equal(session.status, 200)
    assert.equal(mutation.status, 201)
    assert.equal(crossSite.status, 403)
    assert.equal(missingOrigin.status, 403)
    assert.equal(reportPreview.status, 200)
    assert.match(reportPreview.headers.get('content-type') ?? '', /application\/json/)
    const previewPayload = await reportPreview.json()
    assert.ok(previewPayload.days?.length >= 1)
    assert.ok(previewPayload.days[0].cards?.[0]?.payload?.cards?.[0]?.title)
    assert.ok(previewPayload.days[0].cards?.[0]?.payload?.reportMarkdown)
    assert.deepEqual(await mutation.json(), { calls: 1 })
    const count = await fetch(`${backendOrigin}/api/count`)
    assert.deepEqual(await count.json(), { calls: 1 })
  } finally {
    if (vite) await vite.close()
    if (backend.exitCode === null) {
      backend.kill('SIGTERM')
      await once(backend, 'exit')
    }
    await rm(testRoot, { recursive: true, force: true })
    if (previousBackend === undefined) delete process.env.AUDIO_MEMORY_BACKEND_URL
    else process.env.AUDIO_MEMORY_BACKEND_URL = previousBackend
    if (previousDevPort === undefined) delete process.env.AUDIO_MEMORY_DEV_PORT
    else process.env.AUDIO_MEMORY_DEV_PORT = previousDevPort
  }
})


test('development client establishes its health and session boundary before a mutation', async () => {
  const reservedBackendPortServer = createHttpServer()
  const backendPort = await listen(reservedBackendPortServer)
  await close(reservedBackendPortServer)
  const backendOrigin = `http://127.0.0.1:${backendPort}`
  const testRoot = await mkdtemp(join(tmpdir(), 'audio-memory-dev-client-'))
  const backendRoot = fileURLToPath(new URL('../../backend/', import.meta.url))
  const backend = spawn(
    fileURLToPath(new URL('../../backend/.venv/bin/python', import.meta.url)),
    [
      '-m', 'uvicorn', 'dev_proxy_app:app',
      '--app-dir', join(backendRoot, 'tests/support'),
      '--host', '127.0.0.1',
      '--port', String(backendPort),
      '--log-level', 'error',
    ],
    {
      cwd: backendRoot,
      env: {
        ...process.env,
        AUDIO_MEMORY_TEST_SECURITY_DB: join(testRoot, 'security.sqlite3'),
        AUDIO_MEMORY_TEST_BACKEND_PORT: String(backendPort),
        AUDIO_MEMORY_TEST_BACKEND_PROFILE: 'development',
      },
      stdio: 'ignore',
    },
  )
  await waitForBackend(`${backendOrigin}/api/count`, backend)

  const previousExpectedProfile = process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE
  const originalFetch = globalThis.fetch
  process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE = 'development'
  globalThis.fetch = (input, options = {}) => originalFetch(
    typeof input === 'string' && input.startsWith('/') ? `${backendOrigin}${input}` : input,
    options.method === 'POST' ? {
      ...options,
      headers: { ...options.headers, Origin: backendOrigin },
    } : options,
  )
  try {
    const client = await import(`../src/api/client.js?development-runtime=${Date.now()}`)
    assert.deepEqual(await client.apiRequest('/effect', {
      method: 'POST', idempotencyKey: 'development-runtime-action',
    }), { calls: 1 })

    const count = await originalFetch(`${backendOrigin}/api/count`)
    assert.deepEqual(await count.json(), { calls: 1 })
  } finally {
    globalThis.fetch = originalFetch
    if (backend.exitCode === null) {
      backend.kill('SIGTERM')
      await once(backend, 'exit')
    }
    await rm(testRoot, { recursive: true, force: true })
    if (previousExpectedProfile === undefined) delete process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE
    else process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE = previousExpectedProfile
  }
})


test('development client rechecks health after a backend is replaced before reusing a session', async () => {
  const reservedBackendPortServer = createHttpServer()
  const backendPort = await listen(reservedBackendPortServer)
  await close(reservedBackendPortServer)
  const backendOrigin = `http://127.0.0.1:${backendPort}`
  const testRoot = await mkdtemp(join(tmpdir(), 'audio-memory-dev-replacement-'))
  const backendRoot = fileURLToPath(new URL('../../backend/', import.meta.url))
  const backend = spawn(fileURLToPath(new URL('../../backend/.venv/bin/python', import.meta.url)), [
    '-m', 'uvicorn', 'dev_proxy_app:app', '--app-dir', join(backendRoot, 'tests/support'),
    '--host', '127.0.0.1', '--port', String(backendPort), '--log-level', 'error',
  ], { cwd: backendRoot, env: {
    ...process.env,
    AUDIO_MEMORY_TEST_SECURITY_DB: join(testRoot, 'security.sqlite3'),
    AUDIO_MEMORY_TEST_BACKEND_PORT: String(backendPort),
    AUDIO_MEMORY_TEST_BACKEND_PROFILE: 'development',
  }, stdio: 'ignore' })
  await waitForBackend(`${backendOrigin}/api/count`, backend)

  const previousExpectedProfile = process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE
  const originalFetch = globalThis.fetch
  process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE = 'development'
  globalThis.fetch = (input, options = {}) => originalFetch(
    typeof input === 'string' && input.startsWith('/') ? `${backendOrigin}${input}` : input,
    options.method === 'POST' ? { ...options, headers: { ...options.headers, Origin: backendOrigin } } : options,
  )
  try {
    const client = await import(`../src/api/client.js?replacement=${Date.now()}`)
    await client.apiRequest('/effect', { method: 'POST', idempotencyKey: 'before-replacement' })
    const switched = await originalFetch(`${backendOrigin}/api/test-profile/production`)
    assert.equal(switched.status, 200)

    await assert.rejects(
      client.apiRequest('/effect', { method: 'POST', idempotencyKey: 'after-replacement' }),
      { code: 'runtime_environment_blocked', message: /正式环境/ },
    )
    const count = await originalFetch(`${backendOrigin}/api/count`)
    assert.deepEqual(await count.json(), { calls: 1 })
  } finally {
    globalThis.fetch = originalFetch
    if (backend.exitCode === null) { backend.kill('SIGTERM'); await once(backend, 'exit') }
    await rm(testRoot, { recursive: true, force: true })
    if (previousExpectedProfile === undefined) delete process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE
    else process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE = previousExpectedProfile
  }
})


test('development UI blocks a production backend before it creates a session or mutates', { concurrency: false }, async () => {
  const reservedBackendPortServer = createHttpServer()
  const backendPort = await listen(reservedBackendPortServer)
  await close(reservedBackendPortServer)
  const backendOrigin = `http://127.0.0.1:${backendPort}`
  const testRoot = await mkdtemp(join(tmpdir(), 'audio-memory-dev-profile-'))
  const backendRoot = fileURLToPath(new URL('../../backend/', import.meta.url))
  const backend = spawn(
    fileURLToPath(new URL('../../backend/.venv/bin/python', import.meta.url)),
    [
      '-m', 'uvicorn', 'dev_proxy_app:app',
      '--app-dir', join(backendRoot, 'tests/support'),
      '--host', '127.0.0.1',
      '--port', String(backendPort),
      '--log-level', 'error',
    ],
    {
      cwd: backendRoot,
      env: {
        ...process.env,
        AUDIO_MEMORY_TEST_SECURITY_DB: join(testRoot, 'security.sqlite3'),
        AUDIO_MEMORY_TEST_BACKEND_PORT: String(backendPort),
        AUDIO_MEMORY_TEST_BACKEND_PROFILE: 'production',
      },
      stdio: 'ignore',
    },
  )
  await waitForBackend(`${backendOrigin}/api/count`, backend)

  const reservedDevPortServer = createHttpServer()
  const devPort = await listen(reservedDevPortServer)
  await close(reservedDevPortServer)
  const previousBackend = process.env.AUDIO_MEMORY_BACKEND_URL
  const previousDevPort = process.env.AUDIO_MEMORY_DEV_PORT
  const previousExpectedProfile = process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE
  process.env.AUDIO_MEMORY_BACKEND_URL = backendOrigin
  process.env.AUDIO_MEMORY_DEV_PORT = String(devPort)
  process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE = 'development'

  let vite
  const originalFetch = globalThis.fetch
  try {
    vite = await createViteServer({
      configFile: fileURLToPath(new URL('../vite.config.mjs', import.meta.url)),
      logLevel: 'silent',
    })
    await vite.listen()
    const devOrigin = `http://127.0.0.1:${devPort}`
    globalThis.fetch = (input, options = {}) => originalFetch(
      typeof input === 'string' && input.startsWith('/') ? `${devOrigin}${input}` : input,
      options.method === 'POST' ? {
        ...options,
        headers: { ...options.headers, Origin: devOrigin },
      } : options,
    )
    const client = await import(`../src/api/client.js?production-profile-guard=${Date.now()}`)

    await assert.rejects(
      client.apiRequest('/effect', { method: 'POST', idempotencyKey: 'must-not-write' }),
      { code: 'runtime_environment_blocked', message: /正式环境/ },
    )

    const count = await originalFetch(`${backendOrigin}/api/count`)
    assert.deepEqual(await count.json(), { calls: 0 })
  } finally {
    globalThis.fetch = originalFetch
    if (vite) await vite.close()
    if (backend.exitCode === null) {
      backend.kill('SIGTERM')
      await once(backend, 'exit')
    }
    await rm(testRoot, { recursive: true, force: true })
    if (previousBackend === undefined) delete process.env.AUDIO_MEMORY_BACKEND_URL
    else process.env.AUDIO_MEMORY_BACKEND_URL = previousBackend
    if (previousDevPort === undefined) delete process.env.AUDIO_MEMORY_DEV_PORT
    else process.env.AUDIO_MEMORY_DEV_PORT = previousDevPort
    if (previousExpectedProfile === undefined) delete process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE
    else process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE = previousExpectedProfile
  }
})
