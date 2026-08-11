import { expect, test } from '@playwright/test'

test('prompt settings exposes only the two versioned runtime prompts as read-only', async ({ page }) => {
  const prompts = { prompts: [
    { scene_id: 'autonomous-analysis', label: '自主分析', version: 2, content: '自主分析生产规则', editable: false, source: 'versioned-code' },
    { scene_id: 'autonomous-profile', label: '隐藏画像', version: 1, content: '隐藏画像生产规则', editable: false, source: 'versioned-code' },
  ] }
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { days: [], todos: [] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/prompts') return route.fulfill({ json: prompts })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')

  await page.getByRole('button', { name: 'Prompt 设置' }).click()
  await expect(page.getByRole('button', { name: /自主分析/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /隐藏画像/ })).toBeVisible()
  await expect(page.getByText('会议纪要')).toHaveCount(0)
  await expect(page.getByText('当前生产 Prompt，由程序版本化维护。')).toBeVisible()
  await expect(page.locator('.prompt-textarea')).toHaveValue('自主分析生产规则')
  await expect(page.locator('.prompt-textarea')).toHaveAttribute('readonly', '')
  await expect(page.getByRole('button', { name: '编辑' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '保存' })).toHaveCount(0)

  await page.getByRole('button', { name: /隐藏画像/ }).click()
  await expect(page.locator('.prompt-textarea')).toHaveValue('隐藏画像生产规则')
})
