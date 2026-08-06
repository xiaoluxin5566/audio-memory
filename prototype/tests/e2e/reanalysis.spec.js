import { expect, test } from '@playwright/test'

test('history reanalysis previews, starts, shows progress and protects clearing', async ({ page }) => {
  let current = null
  let createdWith = null
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [{ provider_id: 'kimi', display_name: 'Kimi', model_id: 'kimi-k2.5', state: 'available', active: true }] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [{ date: '2026年8月6日', audio: [{ id: 'f1', original_name: '会议.mp3', duration_ms: 1000, uploaded_at: '2026-08-06T10:00:00Z' }] }] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/history/reanalysis-batches/preview') return route.fulfill({ json: { source_batch_count: 1, audio_file_count: 1, transcript_character_count: 3200, provider_display_name: 'Kimi', model_id: 'kimi-k2.5', prompt_summary: { todo: { version: 1 }, meeting: { version: 2 }, parenting: { version: 1 }, content: { version: 1 }, growth: { version: 1 }, inspiration: { version: 1 } }, estimated_calls_min: 6, estimated_calls_max: 8, whisper_calls: 0, blockers: [], preview_token: 'frozen-preview' } })
    if (pathname === '/api/history/reanalysis-batches/current') return route.fulfill({ status: current ? 200 : 204, json: current })
    if (pathname === '/api/history/reanalysis-batches' && request.method() === 'POST') {
      createdWith = request.postDataJSON()
      current = { id: 'reanalysis-1', status: 'running', total: 1, pending: 0, running: 1, succeeded: 0, failed: 0, stopped: 0 }
      return route.fulfill({ status: 201, json: current })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/history')
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByRole('heading', { name: '重新分析历史' })).toBeVisible()
  await expect(page.getByText('1 个上传批次 · 1 个音频文件')).toBeVisible()
  await expect(page.getByText('3,200 个字符')).toBeVisible()
  await expect(page.getByText('Kimi · kimi-k2.5')).toBeVisible()
  await expect(page.getByText('不会重新转写，也不会重新进行说话人识别。')).toBeVisible()
  await page.getByRole('button', { name: '确认重新分析' }).click()

  expect(createdWith).toEqual({ preview_token: 'frozen-preview' })
  await expect(page.getByRole('button', { name: '重新分析中 0/1' })).toBeVisible()
  await expect(page.getByRole('button', { name: '清除所有历史' })).toBeDisabled()
})

test('a terminal batch exposes a fresh run and stopped work can continue', async ({ page }) => {
  let previewReads = 0
  let resumed = false
  let created = false
  const stopped = { id: 'stopped-1', status: 'stopped', total: 3, pending: 0, running: 0, succeeded: 1, failed: 0, stopped: 2 }
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [{ date: '2026年8月6日', audio: [{ id: 'f1', original_name: '会议.mp3', duration_ms: 1000, uploaded_at: '2026-08-06T10:00:00Z' }] }] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/history/reanalysis-batches/current') return route.fulfill({ json: stopped })
    if (pathname === '/api/history/reanalysis-batches/preview') {
      previewReads += 1
      return route.fulfill({ json: { source_batch_count: 1, audio_file_count: 1, transcript_character_count: 20, provider_display_name: 'Kimi', model_id: 'kimi-k2.5', prompt_summary: { todo: { version: 1 }, meeting: { version: 1 }, parenting: { version: 1 }, content: { version: 1 }, growth: { version: 1 }, inspiration: { version: 1 } }, estimated_calls_min: 6, estimated_calls_max: 6, blockers: [], preview_token: 'fresh-preview' } })
    }
    if (pathname === '/api/history/reanalysis-batches/stopped-1/resume') { resumed = true; return route.fulfill({ json: { ...stopped, status: 'running', stopped: 0, pending: 2, running: 1 } }) }
    if (pathname === '/api/history/reanalysis-batches' && request.method() === 'POST') { created = true; return route.fulfill({ status: 201, json: { ...stopped, id: 'new-run', status: 'running', stopped: 0, pending: 1, running: 1 } }) }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/settings/prompts')
  await expect(page.getByRole('button', { name: '清除所有历史' })).toBeEnabled()
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByRole('button', { name: '继续剩余项目' })).toBeVisible()
  await page.getByRole('button', { name: '继续剩余项目' }).click()
  expect(resumed).toBe(true)
  await page.reload()
  await expect(page.getByRole('button', { name: '清除所有历史' })).toBeEnabled()
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByRole('button', { name: '确认重新分析' })).toBeVisible()
  await page.getByRole('button', { name: '确认重新分析' }).click()
  expect(previewReads).toBeGreaterThanOrEqual(2)
  expect(created).toBe(true)
})

test('clearing terminal history immediately disables the reanalysis entry', async ({ page }) => {
  let cleared = false
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: cleared ? { todos: [], days: [] } : { todos: [], days: [] } })
    if (pathname === '/api/history' && request.method() === 'DELETE') { cleared = true; return route.fulfill({ status: 204 }) }
    if (pathname === '/api/history') return route.fulfill({ json: cleared ? { days: [] } : { days: [{ date: '2026年8月6日', audio: [{ id: 'f1', original_name: '会议.mp3', duration_ms: 1000, uploaded_at: '2026-08-06T10:00:00Z' }] }] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/history/reanalysis-batches/current') return route.fulfill({ json: cleared ? null : { id: 'finished-1', status: 'completed', total: 1, pending: 0, running: 0, succeeded: 1, failed: 0, stopped: 0 } })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/history')
  await expect(page.getByRole('button', { name: '重新分析历史' })).toBeEnabled()
  await page.getByRole('button', { name: '清除所有历史' }).click()
  await page.getByRole('button', { name: '永久清除' }).click()
  await expect(page.getByRole('button', { name: '重新分析历史' })).toBeDisabled()
})

test('terminal preview hides stale costs and disables confirmation until a fresh preview arrives', async ({ page }) => {
  let previewReads = 0
  let releaseFreshPreview
  const freshPreview = new Promise((resolve) => { releaseFreshPreview = resolve })
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [{ date: '2026年8月6日', audio: [{ id: 'f1', original_name: '会议.mp3', duration_ms: 1000, uploaded_at: '2026-08-06T10:00:00Z' }] }] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/history/reanalysis-batches/current') return route.fulfill({ json: { id: 'finished-1', status: 'completed', total: 1, pending: 0, running: 0, succeeded: 1, failed: 0, stopped: 0 } })
    if (pathname === '/api/history/reanalysis-batches/preview') {
      previewReads += 1
      if (previewReads === 2) await freshPreview
      if (previewReads === 3) return route.fulfill({ status: 500, json: { detail: { message: '预览读取失败' } } })
      return route.fulfill({ json: previewReads === 1
        ? { source_batch_count: 1, audio_file_count: 1, transcript_character_count: 10, provider_display_name: 'OldModel', model_id: 'old-1', prompt_summary: { todo: { version: 1 } }, estimated_calls_min: 1, estimated_calls_max: 1, blockers: [], preview_token: 'old-token' }
        : { source_batch_count: 1, audio_file_count: 2, transcript_character_count: 20, provider_display_name: 'FreshModel', model_id: 'fresh-2', prompt_summary: { todo: { version: 2 } }, estimated_calls_min: 2, estimated_calls_max: 3, blockers: [], preview_token: 'fresh-token' } })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/')
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByText('OldModel · old-1')).toBeVisible()
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByText('正在读取本次重新分析范围…')).toBeVisible()
  await expect(page.getByRole('button', { name: '确认重新分析' })).toBeDisabled()
  await expect(page.getByText('OldModel · old-1')).toHaveCount(0)
  releaseFreshPreview()
  await expect(page.getByText('FreshModel · fresh-2')).toBeVisible()
  await expect(page.getByRole('button', { name: '确认重新分析' })).toBeEnabled()
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByText('预览读取失败')).toBeVisible()
  await expect(page.getByText('FreshModel · fresh-2')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认重新分析' })).toBeDisabled()
})

test('a late first preview cannot replace the fresh preview after close and reopen', async ({ page }) => {
  let previewReads = 0
  let releaseOldPreview
  const oldPreview = new Promise((resolve) => { releaseOldPreview = resolve })
  let createdBody = null
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [{ date: '2026年8月6日', audio: [{ id: 'f1', original_name: '会议.mp3', duration_ms: 1000, uploaded_at: '2026-08-06T10:00:00Z' }] }] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/history/reanalysis-batches/current') return route.fulfill({ json: { id: 'finished-1', status: 'completed', total: 1, pending: 0, running: 0, succeeded: 1, failed: 0, stopped: 0 } })
    if (pathname === '/api/history/reanalysis-batches/preview') {
      const previewIndex = ++previewReads
      if (previewIndex === 1) await oldPreview
      return route.fulfill({ json: previewIndex === 1
        ? { source_batch_count: 1, audio_file_count: 1, transcript_character_count: 10, provider_display_name: 'StaleModel', model_id: 'stale-1', prompt_summary: { todo: { version: 1 } }, estimated_calls_min: 1, estimated_calls_max: 1, blockers: [], preview_token: 'stale-token' }
        : { source_batch_count: 1, audio_file_count: 1, transcript_character_count: 20, provider_display_name: 'FreshModel', model_id: 'fresh-2', prompt_summary: { todo: { version: 2 } }, estimated_calls_min: 2, estimated_calls_max: 2, blockers: [], preview_token: 'fresh-token' } })
    }
    if (pathname === '/api/history/reanalysis-batches' && request.method() === 'POST') { createdBody = request.postDataJSON(); return route.fulfill({ status: 201, json: { id: 'new-1', status: 'running', total: 1, pending: 0, running: 1, succeeded: 0, failed: 0, stopped: 0 } }) }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/')
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByText('正在读取本次重新分析范围…')).toBeVisible()
  await page.getByRole('button', { name: '关闭重新分析' }).click()
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByText('FreshModel · fresh-2')).toBeVisible()
  releaseOldPreview()
  await expect(page.getByText('StaleModel · stale-1')).toHaveCount(0)
  await page.getByRole('button', { name: '确认重新分析' }).click()
  expect(createdBody).toEqual({ preview_token: 'fresh-token' })
})

test('closing the modal preserves a slow initial running status response', async ({ page }) => {
  let releaseCurrent
  const slowCurrent = new Promise((resolve) => { releaseCurrent = resolve })
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [{ date: '2026年8月6日', audio: [{ id: 'f1', original_name: '会议.mp3', duration_ms: 1000, uploaded_at: '2026-08-06T10:00:00Z' }] }] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/history/reanalysis-batches/current') {
      await slowCurrent
      return route.fulfill({ json: { id: 'running-1', status: 'running', total: 4, pending: 2, running: 1, succeeded: 1, failed: 0, stopped: 0 } })
    }
    if (pathname === '/api/history/reanalysis-batches/preview') return route.fulfill({ json: { source_batch_count: 1, audio_file_count: 1, transcript_character_count: 10, provider_display_name: 'Kimi', model_id: 'kimi-k2.5', prompt_summary: { todo: { version: 1 } }, estimated_calls_min: 1, estimated_calls_max: 1, blockers: [], preview_token: 'preview-token' } })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/history')
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await expect(page.getByRole('heading', { name: '重新分析历史' })).toBeVisible()
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  releaseCurrent()

  await expect(page.getByRole('button', { name: '重新分析中 1/4' })).toBeVisible()
  await expect(page.getByRole('button', { name: '清除所有历史' })).toBeDisabled()
})

test('clearing after closing a slow request ignores late preview and current responses', async ({ page }) => {
  let releaseCurrent
  let releasePreview
  const slowCurrent = new Promise((resolve) => { releaseCurrent = resolve })
  const slowPreview = new Promise((resolve) => { releasePreview = resolve })
  let cleared = false
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [] } })
    if (pathname === '/api/history' && request.method() === 'DELETE') { cleared = true; return route.fulfill({ status: 204 }) }
    if (pathname === '/api/history') return route.fulfill({ json: cleared ? { days: [] } : { days: [{ date: '2026年8月6日', audio: [{ id: 'f1', original_name: '会议.mp3', duration_ms: 1000, uploaded_at: '2026-08-06T10:00:00Z' }] }] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/history/reanalysis-batches/current') { await slowCurrent; return route.fulfill({ json: { id: 'late-finished', status: 'completed', total: 1, pending: 0, running: 0, succeeded: 1, failed: 0, stopped: 0 } }) }
    if (pathname === '/api/history/reanalysis-batches/preview') { await slowPreview; return route.fulfill({ json: { source_batch_count: 1, audio_file_count: 1, transcript_character_count: 10, provider_display_name: 'LateModel', model_id: 'late-1', prompt_summary: { todo: { version: 1 } }, estimated_calls_min: 1, estimated_calls_max: 1, blockers: [], preview_token: 'late-token' } }) }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/history')
  await expect(page.getByRole('button', { name: '重新分析历史' })).toBeEnabled()
  await page.getByRole('button', { name: '重新分析历史' }).click()
  await page.getByRole('button', { name: '关闭重新分析' }).click()
  await page.getByRole('button', { name: '清除所有历史' }).click()
  await page.getByRole('button', { name: '永久清除' }).click()
  releasePreview()
  releaseCurrent()
  await expect(page.getByRole('button', { name: '重新分析历史' })).toBeDisabled()
  await expect(page.getByText('LateModel · late-1')).toHaveCount(0)
})
