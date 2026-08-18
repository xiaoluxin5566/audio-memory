import { expect, test } from '@playwright/test'


const providers = {
  providers: [{
    provider_id: 'deepseek',
    display_name: 'DeepSeek',
    state: 'available',
    active: true,
    last_validated_at: '2026-08-18T10:00:00Z',
    error_code: null,
    error_message: null,
    cooldown_until: null,
  }],
}


async function installApi(page, { initiallyEnabled = false, initialStatus = 'inactive', startStatus = null } = {}) {
  let enabled = initiallyEnabled
  let startCalls = 0
  let updateCalls = 0
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: providers })
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/settings/analysis' && request.method() === 'GET') {
      return route.fulfill({ json: { prevent_sleep: enabled, sleep_prevention_status: initialStatus } })
    }
    if (pathname === '/api/settings/analysis' && request.method() === 'PUT') {
      updateCalls += 1
      enabled = request.postDataJSON().prevent_sleep
      return route.fulfill({ json: { prevent_sleep: enabled, sleep_prevention_status: 'inactive' } })
    }
    if (pathname === '/api/jobs' && request.method() === 'POST') {
      return route.fulfill({ status: 201, json: { id: 'job-1', stage: 'uploading' } })
    }
    if (pathname === '/api/jobs/job-1/files' && request.method() === 'POST') {
      return route.fulfill({ status: 201, json: { id: 'file-1', extension: '.mp3' } })
    }
    if (pathname === '/api/jobs/job-1/start' && request.method() === 'POST') {
      startCalls += 1
      return route.fulfill({ json: { id: 'job-1', stage: 'transcribing', sleep_prevention_status: startStatus || (enabled ? 'active' : 'disabled') } })
    }
    if (pathname === '/api/jobs/job-1') {
      return route.fulfill({ json: { id: 'job-1', stage: 'transcribing' } })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  return {
    startCalls: () => startCalls,
    updateCalls: () => updateCalls,
  }
}


async function uploadOneFile(page) {
  await page.goto('/')
  await page.locator('input[type=file]').setInputFiles({
    name: 'meeting.mp3',
    mimeType: 'audio/mpeg',
    buffer: Buffer.from('audio'),
  })
}


test('disabled protection asks before analysis and can be enabled permanently', async ({ page }) => {
  const calls = await installApi(page)
  await uploadOneFile(page)

  await page.getByRole('button', { name: '开始分析 1 个文件' }).click()

  await expect(page.getByRole('dialog', { name: '分析期间保持电脑唤醒？' })).toBeVisible()
  expect(calls.startCalls()).toBe(0)
  await page.getByRole('button', { name: '开启并继续' }).click()

  await expect(page.getByRole('switch', { name: '分析期间保持电脑唤醒' })).toBeChecked()
  expect(calls.updateCalls()).toBe(1)
  expect(calls.startCalls()).toBe(1)
})


test('enabled protection starts analysis without another prompt', async ({ page }) => {
  const calls = await installApi(page, { initiallyEnabled: true })
  await uploadOneFile(page)

  await page.getByRole('button', { name: '开始分析 1 个文件' }).click()

  await expect(page.getByRole('dialog', { name: '分析期间保持电脑唤醒？' })).toBeHidden()
  expect(calls.startCalls()).toBe(1)
})


test('user can continue one analysis without enabling protection', async ({ page }) => {
  const calls = await installApi(page)
  await uploadOneFile(page)

  await page.getByRole('button', { name: '开始分析 1 个文件' }).click()
  await page.getByRole('button', { name: '暂不开启' }).click()

  await expect(page.getByRole('switch', { name: '分析期间保持电脑唤醒' })).not.toBeChecked()
  expect(calls.updateCalls()).toBe(0)
  expect(calls.startCalls()).toBe(1)
})


test('unavailable protection remains visibly warned after page load', async ({ page }) => {
  await installApi(page, { initiallyEnabled: true, initialStatus: 'unavailable' })

  await page.goto('/')

  await expect(page.getByText('防休眠未生效，请保持电脑唤醒', { exact: true })).toBeVisible()
})
