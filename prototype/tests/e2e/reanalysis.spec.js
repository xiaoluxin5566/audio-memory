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
