import { expect, test } from '@playwright/test'

const activeProviders = {
  providers: ['kimi', 'deepseek', 'openai'].map((provider_id) => ({
    provider_id,
    display_name: provider_id === 'kimi' ? 'Kimi' : provider_id === 'deepseek' ? 'DeepSeek' : 'OpenAI',
    state: provider_id === 'deepseek' ? 'available' : 'unconfigured',
    active: provider_id === 'deepseek',
    last_validated_at: provider_id === 'deepseek' ? '2026-08-05T10:00:00Z' : null,
    error_code: null,
    error_message: null,
    cooldown_until: null,
  })),
}

const completedFeed = {
  todos: [],
  days: [{
    date: '2026年8月5日',
    cards: [{
      id: 'card-1',
      batch_id: 'batch-1',
      scene_id: 'meeting',
      uploaded_at: '2026-08-05T10:05:00Z',
      payload: {
        scene_id: 'meeting',
        cards: [{
          card: { title: '产品方案评审', summary: '团队确认了第一阶段的核心体验。' },
          detail: {},
          external_source_ids: [],
        }],
      },
      qa: [],
    }],
  }],
}

const completedHistory = {
  days: [{
    date: '2026年8月5日',
    audio: [{
      id: 'audio-1',
      original_name: 'meeting.mp3',
      duration_ms: 65_000,
      uploaded_at: '2026-08-05T10:05:00Z',
    }],
  }],
}

async function installJobApi(page) {
  let completed = false
  let completedFeedReads = 0
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: activeProviders })
    if (pathname === '/api/feed') {
      if (completed) completedFeedReads += 1
      return route.fulfill({ json: completed ? completedFeed : { days: [], todos: [] } })
    }
    if (pathname === '/api/history') return route.fulfill({ json: completed ? completedHistory : { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/settings/analysis') return route.fulfill({ json: { prevent_sleep: true, sleep_prevention_status: 'inactive' } })
    if (pathname === '/api/jobs' && request.method() === 'POST') {
      return route.fulfill({ json: { id: 'job-1', stage: 'uploading' } })
    }
    if (pathname === '/api/jobs/job-1/files' && request.method() === 'POST') {
      return route.fulfill({ status: 201, json: { id: 'file-1', extension: '.mp3' } })
    }
    if (pathname === '/api/jobs/job-1/start' && request.method() === 'POST') {
      completed = true
      return route.fulfill({ json: { id: 'job-1', stage: 'transcribing' } })
    }
    if (pathname === '/api/jobs/job-1' && request.method() === 'GET') {
      return route.fulfill({ json: { id: 'job-1', stage: 'completed' } })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  return () => completedFeedReads
}

test('completed batch clears upload area and appears in feed and history', async ({ page }) => {
  const completedFeedReads = await installJobApi(page)
  await page.goto('/')

  await page.locator('input[type=file]').setInputFiles({
    name: 'meeting.mp3',
    mimeType: 'audio/mpeg',
    buffer: Buffer.from('browser acceptance audio'),
  })
  await expect(page.getByText('meeting.mp3')).toBeVisible()
  await expect(page.getByText('上传完成')).toBeVisible()

  await page.getByRole('button', { name: '开始分析 1 个文件' }).click()
  await expect.poll(completedFeedReads).toBeGreaterThan(0)
  await expect(page.getByRole('heading', { name: '产品方案评审' })).toBeVisible()
  await expect(page.getByText('meeting.mp3')).toBeHidden()

  await page.getByRole('button', { name: '音频历史' }).click()
  await expect(page.getByText('meeting.mp3')).toBeVisible()
  await expect(page.getByText('1分05秒')).toBeVisible()
})

test('unsupported file pauses later uploads and removing it resumes the queue', async ({ page }) => {
  let uploadCalls = 0
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: activeProviders })
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs' && request.method() === 'POST') {
      return route.fulfill({ json: { id: 'job-pause', stage: 'uploading' } })
    }
    if (pathname === '/api/jobs/job-pause/files' && request.method() === 'POST') {
      uploadCalls += 1
      if (uploadCalls === 2) {
        return route.fulfill({
          status: 415,
          json: { detail: { code: 'unsupported_format', message: '不支持该文件格式，请上传 MP3、AAC 格式文件', file_id: 'bad-file' } },
        })
      }
      return route.fulfill({ status: 201, json: { id: `file-${uploadCalls}`, extension: uploadCalls === 3 ? '.aac' : '.mp3' } })
    }
    if (pathname === '/api/jobs/job-pause/files/bad-file' && request.method() === 'DELETE') {
      return route.fulfill({ status: 204 })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')

  await page.locator('input[type=file]').setInputFiles([
    { name: 'first.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('first') },
    { name: 'broken.wav', mimeType: 'audio/wav', buffer: Buffer.from('broken') },
    { name: 'third.aac', mimeType: 'audio/aac', buffer: Buffer.from('third') },
  ])

  await expect(page.getByText('不支持该文件格式，请上传 MP3、AAC 格式文件')).toBeVisible()
  expect(uploadCalls).toBe(2)
  await page.getByRole('button', { name: '移除 broken.wav' }).click()
  await expect(page.getByText('third.aac')).toBeVisible()
  await expect(page.getByText('third.aac').locator('..')).toContainText('上传完成')
  expect(uploadCalls).toBe(3)
})
