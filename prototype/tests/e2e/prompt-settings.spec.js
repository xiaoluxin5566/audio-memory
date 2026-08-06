import { expect, test } from '@playwright/test'

test('editing a fixed scene saves a new prompt version for future analysis', async ({ page }) => {
  let version = 1
  let content = '识别会议并输出核心结论。'
  let savedBody = null
  const prompts = () => ({
    prompts: ['todo', 'meeting', 'parenting', 'content', 'growth', 'inspiration'].map((scene_id) => ({
      scene_id,
      version: scene_id === 'meeting' ? version : 1,
      content: scene_id === 'meeting' ? content : `${scene_id} 默认提示词`,
    })),
  })
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/prompts/meeting' && request.method() === 'PUT') {
      savedBody = request.postDataJSON()
      content = savedBody.content
      version += 1
      return route.fulfill({ json: { scene_id: 'meeting', version, content } })
    }
    if (pathname === '/api/prompts') return route.fulfill({ json: prompts() })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')

  await page.getByRole('button', { name: 'Prompt 设置' }).click()
  await page.getByRole('button', { name: /会议纪要/ }).click()
  await page.getByRole('button', { name: '编辑' }).click()
  const editor = page.locator('.prompt-textarea')
  await editor.fill('识别会议，重点输出决策与待办。')
  await page.getByRole('button', { name: '保存' }).click()

  await expect(page.getByText('Prompt 已保存，新分析将使用该版本')).toBeVisible()
  await expect(page.getByRole('button', { name: /会议纪要/ })).toContainText('v2')
  expect(savedBody).toEqual({ expected_version: 1, content: '识别会议，重点输出决策与待办。' })
})
