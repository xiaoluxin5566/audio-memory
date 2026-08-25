import { expect, test } from '@playwright/test'

test('autonomous analysis uses editorial sections with optional timeline and viewpoint matrix', async ({ page }) => {
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [{ provider_id: 'deepseek', display_name: 'DeepSeek', state: 'available', active: true }] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [{
      date: '2026年8月11日',
      cards: [{
        id: 'analysis-1', batch_id: 'batch-1', scene_id: 'analysis', uploaded_at: '2026-08-11T10:00:00Z', qa: [], evidence: [],
        payload: { scene_id: 'analysis', cards: [{
          title: '工作系统的长期错配', summary: '目标、权责与能力使用方式同时失衡。真正需要筛选的是一套能把产品判断转化为结果的工作系统。',
          content: [
            { type: 'scene_reconstruction', title: '场景还原与核心观点', body: '午间交流逐渐从资源问题转向组织机制。\n\n- 目标持续摇摆\n- 关键资源需要等待', items: [] },
            { type: 'analysis', title: '分析、问题与点评', body: '**危险循环**\n\n1. 不认同方向\n2. 说服失败\n3. 降低投入\n\n不认同方向 → 说服失败 → 降低投入 → 结果变差\n\n**三个并列风险**\n\n1. 行业脱节：需要补课\n2. 逃离投射：可能美化机会\n3. 经验错配：方法不能直接复用\n\n**双方真正关心的事**\n\n| 维度 | 其中一方 | 另一方 |\n| --- | --- | --- |\n| 核心诉求 | 产品目标清晰 | 尽快推进 |', items: [] },
          ],
          quotes: [],
          recommendations: [{ title: '建立最小闭环', reason: '保护交付质量', actions: ['写清目标和约束'], suggested_language: '请确认方向。', success_signal: '得到书面确认', caveat: null }],
        }] },
      }],
    }] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/')
  await page.getByRole('heading', { name: '工作系统的长期错配' }).click()

  await expect(page.locator('.analysis-hero')).toContainText('目标、权责与能力使用方式同时失衡')
  await expect(page.locator('.autonomous-editorial')).toHaveCount(2)
  await expect(page.locator('.analysis-timeline')).toContainText('不认同方向')
  await expect(page.locator('.analysis-cause-chain')).toContainText('结果变差')
  await expect(page.locator('.analysis-insight-grid')).toContainText('行业脱节')
  await expect(page.locator('.analysis-matrix')).toContainText('产品目标清晰')
  await expect(page.locator('.analysis-recommendation-list')).toContainText('建立最小闭环')
  await expect(page.locator('.analysis-relationship-map')).toHaveCount(0)
})

test('desktop report scrolling keeps the control rail fixed without its own scroll', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 600 })
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [{ provider_id: 'deepseek', display_name: 'DeepSeek', model_id: 'deepseek-v4-pro', state: 'available', active: true }] } })
    if (pathname === '/api/asr') return route.fulfill({ json: { provider_id: 'volcano', display_name: '火山语音', resource_id: 'volc.seedasr.auc', state: 'available', last_validated_at: '2026-08-24T10:00:00Z' } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [{
      date: '2026年8月25日', cards: [{
        id: 'long-report', batch_id: 'batch-1', scene_id: 'analysis', uploaded_at: '2026-08-25T10:00:00Z', qa: [], evidence: [],
        payload: { scene_id: 'analysis', cards: [{
          title: '长报告滚动测试', summary: '验证左栏固定。',
          content: Array.from({ length: 18 }, (_, index) => ({ type: 'analysis', title: `分析 ${index + 1}`, body: '这是一段用于形成足够页面高度的报告正文。'.repeat(8), items: [] })),
          quotes: [], recommendations: [],
        }] },
      }],
    }] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/settings/analysis') return route.fulfill({ json: { prevent_sleep: false, status: 'inactive' } })
    if (pathname === '/api/history/reanalysis-batches/current') return route.fulfill({ status: 204 })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')
  await page.getByRole('heading', { name: '长报告滚动测试' }).click()

  const rail = page.locator('.control-rail')
  const before = await rail.evaluate((element) => element.getBoundingClientRect().top)
  await page.evaluate(() => window.scrollTo(0, 900))
  const after = await rail.evaluate((element) => element.getBoundingClientRect().top)
  const overflowY = await rail.evaluate((element) => getComputedStyle(element).overflowY)

  expect(after).toBe(before)
  expect(overflowY).toBe('visible')
})
