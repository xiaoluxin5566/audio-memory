import { expect, test } from '@playwright/test'

test('opening the page restores an interrupted analysis and can resume it', async ({ page }) => {
  let resumed = false
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') {
      return route.fulfill({ json: { providers: [{ provider_id: 'deepseek', display_name: 'DeepSeek', state: 'available', active: true }] } })
    }
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') {
      return route.fulfill({ json: {
        id: 'job-recovery',
        stage: 'interrupted',
        error_code: null,
        provider_id: 'deepseek',
        model_id: 'deepseek-chat',
        files: [{ id: 'file-1', original_name: 'unfinished.mp3', extension: '.mp3', size_bytes: 2048, upload_progress: 100 }],
      } })
    }
    if (pathname === '/api/jobs/job-recovery/resume' && request.method() === 'POST') {
      resumed = true
      return route.fulfill({ status: 202, json: { id: 'job-recovery', stage: 'transcribing' } })
    }
    if (pathname === '/api/jobs/job-recovery' && request.method() === 'GET') {
      return route.fulfill({ json: {
        id: 'job-recovery', stage: resumed ? 'transcribing' : 'interrupted',
        progress_percent: resumed ? 19 : 3,
        eta_state: resumed ? 'estimating' : 'unavailable',
        eta_seconds: null,
      } })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/')

  await expect(page.getByText('发现未完成的分析任务')).toBeVisible()
  await expect(page.getByText('unfinished.mp3')).toBeVisible()
  await page.getByRole('button', { name: '继续分析' }).click()
  await expect(page.getByText('本地 Whisper 转写中')).toBeVisible()
  await expect(page.locator('.job-title span')).toHaveText('19%')
  await expect(page.getByText('正在估算剩余时间…')).toBeVisible()
  expect(resumed).toBe(true)
})

for (const errorCode of ['credential_changed', 'fixed_rules_changed']) {
  test(`a failed ${errorCode} analysis retries without returning to Whisper`, async ({ page }) => {
    let retried = false
    await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
      const request = route.request()
      const { pathname } = new URL(request.url())
      if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
      if (pathname === '/api/providers') {
        return route.fulfill({ json: { providers: [{ provider_id: 'deepseek', display_name: 'DeepSeek', state: 'available', active: true }] } })
      }
      if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
      if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
      if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
      if (pathname === '/api/jobs/active') {
        return route.fulfill({ json: {
          id: `job-${errorCode}`,
          stage: 'failed',
          error_code: errorCode,
          provider_id: 'deepseek',
          model_id: 'deepseek-chat',
          files: [{ id: 'file-1', original_name: 'retained.mp3', extension: '.mp3', size_bytes: 2048, upload_progress: 100 }],
        } })
      }
      if (pathname === `/api/jobs/job-${errorCode}/retry-analysis` && request.method() === 'POST') {
        retried = true
        return route.fulfill({ status: 202, json: { id: `job-${errorCode}`, stage: 'analyzing' } })
      }
      if (pathname === `/api/jobs/job-${errorCode}` && request.method() === 'GET') {
        return route.fulfill({ json: { id: `job-${errorCode}`, stage: retried ? 'analyzing' : 'failed', progress_percent: 80 } })
      }
      return route.fulfill({ status: 404, json: { detail: 'not found' } })
    })

    await page.goto('/')
    await expect(page.getByText('模型分析失败')).toBeVisible()
    await page.getByRole('button', { name: '重新分析', exact: true }).click()
    await expect(page.getByText('模型正在分析内容')).toBeVisible()
    await expect(page.getByText('本地 Whisper 转写中')).toHaveCount(0)
    expect(retried).toBe(true)
  })
}
