import { expect, test } from '@playwright/test'

const emptyProviders = () => ({
  providers: ['kimi', 'deepseek', 'openai'].map((provider_id) => ({
    provider_id,
    display_name: provider_id === 'kimi' ? 'Kimi' : provider_id === 'deepseek' ? 'DeepSeek' : 'OpenAI',
    model_id: provider_id === 'kimi' ? 'kimi-k2.5' : provider_id === 'deepseek' ? 'deepseek-v4-flash' : 'gpt-5-mini',
    state: 'unconfigured',
    active: provider_id === 'kimi',
    last_validated_at: null,
    error_code: null,
    error_message: null,
    cooldown_until: null,
  })),
})

const configuredDeepSeek = () => ({
  providers: emptyProviders().providers.map((provider) => provider.provider_id === 'deepseek'
    ? { ...provider, state: 'available', active: true, last_validated_at: '2026-08-05T10:00:00Z' }
    : { ...provider, active: false }),
})

async function installApi(page, { rejectDeepSeek = false } = {}) {
  let providers = emptyProviders()
  const calls = []
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push(`${request.method()} ${url.pathname}`)
    if (url.pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (url.pathname === '/api/providers' && request.method() === 'GET') {
      return route.fulfill({ json: providers })
    }
    if (url.pathname === '/api/asr') return route.fulfill({ json: { provider_id: 'volcano', display_name: '火山语音', resource_id: 'volc.seedasr.auc', state: 'available', last_validated_at: '2026-08-23T10:00:00Z', error_code: null } })
    if (url.pathname === '/api/providers/deepseek/key' && request.method() === 'PUT') {
      if (rejectDeepSeek) {
        return route.fulfill({
          status: 401,
          json: { detail: { code: 'invalid_key', message: 'API Key 无效，请重新填写' } },
        })
      }
      providers = configuredDeepSeek()
      return route.fulfill({ json: { provider_id: 'deepseek', state: 'available' } })
    }
    if (url.pathname === '/api/providers/deepseek/activate' && request.method() === 'POST') {
      return route.fulfill({ json: { provider_id: 'deepseek', active: true } })
    }
    if (url.pathname.startsWith('/api/providers/deepseek/candidate/') && request.method() === 'DELETE') {
      return route.fulfill({ status: 204 })
    }
    if (url.pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (url.pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (url.pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  return calls
}

test('successful first configuration becomes current and closes the modal', async ({ page }) => {
  const calls = await installApi(page)
  await page.goto('/')

  await expect(page.getByRole('heading', { name: '先上传音频' })).toBeVisible()
  await page.locator('section').filter({ hasText: '模型与 API Key' }).getByRole('button', { name: '去配置' }).click()
  await page.getByRole('button', { name: 'DeepSeek' }).click()
  await page.getByLabel('API Key').fill('visible-test-key')
  await page.getByRole('button', { name: '保存并校验' }).click()

  await expect(page.getByRole('heading', { name: '配置分析模型' })).toBeHidden()
  await expect(page.locator('section').filter({ hasText: '模型与 API Key' }).locator('.provider-summary b')).toHaveText('deepseek-v4-flash')
  await expect(page.locator('section').filter({ hasText: '模型与 API Key' }).getByText('连接可用', { exact: false })).toBeVisible()
  expect(calls).toContain('PUT /api/providers/deepseek/key')
  expect(calls).toContain('POST /api/providers/deepseek/activate')
  expect(calls.indexOf('PUT /api/providers/deepseek/key')).toBeLessThan(calls.indexOf('POST /api/providers/deepseek/activate'))
})

test('failed configuration keeps the visible key until the modal is closed', async ({ page }) => {
  const calls = await installApi(page, { rejectDeepSeek: true })
  await page.goto('/')
  await page.locator('section').filter({ hasText: '模型与 API Key' }).getByRole('button', { name: '去配置' }).click()
  await page.getByRole('button', { name: 'DeepSeek' }).click()

  const keyField = page.getByLabel('API Key')
  await expect(keyField).toHaveAttribute('type', 'text')
  await keyField.fill('visible-invalid-key')
  await page.getByRole('button', { name: '保存并校验' }).click()

  await expect(page.getByText('API Key 无效，请重新填写')).toBeVisible()
  await expect(keyField).toHaveValue('visible-invalid-key')
  await page.getByRole('button', { name: '取消' }).click()
  await expect(page.getByRole('heading', { name: '配置分析模型' })).toBeHidden()
  expect(calls.some((call) => call.startsWith('DELETE /api/providers/deepseek/candidate/'))).toBe(true)
})

test('startup validation refreshes automatically without manual revalidation', async ({ page }) => {
  let providerReads = 0
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') {
      providerReads += 1
      return route.fulfill({ json: { providers: [{
        provider_id: 'deepseek', display_name: 'DeepSeek', active: true,
        state: providerReads <= 2 ? 'validating' : 'available',
        last_validated_at: providerReads <= 2 ? null : '2026-08-05T10:00:00Z',
      }] } })
    }
    if (pathname === '/api/asr') return route.fulfill({ json: { provider_id: 'volcano', display_name: '火山语音', resource_id: 'volc.seedasr.auc', state: 'available', last_validated_at: '2026-08-23T10:00:00Z', error_code: null } })
    if (pathname === '/api/providers/validate-configured' && request.method() === 'POST') {
      return route.fulfill({ json: { providers: [{
        provider_id: 'deepseek', display_name: 'DeepSeek', active: true,
        state: 'validating', last_validated_at: null,
      }] } })
    }
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')

  await expect(page.getByText('连接可用', { exact: false })).toBeVisible({ timeout: 5_000 })
  await expect(page.locator('input[type=file]')).toBeEnabled()
  expect(providerReads).toBeGreaterThanOrEqual(3)
})

test('initial page load validates configured providers once across route changes', async ({ page }) => {
  let providers = { providers: [{
    provider_id: 'deepseek', display_name: 'DeepSeek', active: true,
    state: 'validating', last_validated_at: null,
  }] }
  const calls = []
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    calls.push(`${request.method()} ${pathname}`)
    if (pathname === '/api/providers' && request.method() === 'GET') {
      return route.fulfill({ json: providers })
    }
    if (pathname === '/api/providers/validate-configured' && request.method() === 'POST') {
      providers = configuredDeepSeek()
      return route.fulfill({ json: providers })
    }
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')

  await expect.poll(() => calls.filter((call) => call === 'POST /api/providers/validate-configured').length).toBe(1)
  await expect(page.getByText('连接可用', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '音频历史' }).click()
  await expect(page.getByRole('heading', { name: '音频历史' })).toBeVisible()
  await page.getByRole('button', { name: '信息流' }).click()
  await expect(page.getByRole('heading', { name: '先上传音频' })).toBeVisible()
  await expect.poll(() => calls.filter((call) => call === 'POST /api/providers/validate-configured').length).toBe(1)
})

test('overdue todo remains unchecked and saves a local deadline as ISO', async ({ page }) => {
  let todo = {
    id: 'todo-1', text: '整理会议结论', due_at: '2026-08-04T08:00:00+00:00', completed: false, overdue: true,
  }
  const updates = []
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: emptyProviders() })
    if (pathname === '/api/providers/validate-configured') return route.fulfill({ json: emptyProviders() })
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [todo] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/todos/todo-1' && request.method() === 'PATCH') {
      const update = request.postDataJSON()
      updates.push(update)
      todo = { ...todo, ...update, due_at: update.due_at || null, overdue: false }
      return route.fulfill({ json: todo })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')

  await expect(page.getByText('已逾期')).toBeVisible()
  await expect(page.locator('.todo-check')).not.toHaveClass(/checked/)
  await page.getByRole('button', { name: '编辑' }).click()
  await page.getByLabel('截止时间').fill('2026-08-06T09:30')
  await page.getByRole('button', { name: '保存' }).click()

  await expect.poll(() => updates.length).toBe(1)
  expect(updates[0].due_at).toMatch(/^2026-08-06T.*Z$/)
})
