import { expect, test } from '@playwright/test'

function feedPayload(todoText = '整理会议结论', completed = false, cleared = false) {
  if (cleared) return { days: [], todos: [] }
  return {
    todos: [{ id: 'todo-1', text: todoText, due_at: null, completed }],
    days: [{
      date: '2026年8月5日',
      cards: [{
        id: 'card-1',
        batch_id: 'batch-1',
        scene_id: 'meeting',
        uploaded_at: '2026-08-05T10:05:00Z',
        payload: {
          card: { title: '周会重点复盘', summary: '本次讨论明确了第一阶段的交付边界。' },
          detail_sections: [{ kind: 'text', title: '核心结论', text: '优先跑通真实使用流程。' }],
        },
        qa: [],
      }],
    }],
  }
}

test('todo, card detail and clear-history actions remain connected while feedback is hidden', async ({ page }) => {
  let todoText = '整理会议结论'
  let completed = false
  let cleared = false
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') {
      return route.fulfill({ json: { providers: [{ provider_id: 'deepseek', display_name: 'DeepSeek', state: 'available', active: true }] } })
    }
    if (pathname === '/api/feed') return route.fulfill({ json: feedPayload(todoText, completed, cleared) })
    if (pathname === '/api/history' && request.method() === 'DELETE') {
      cleared = true
      return route.fulfill({ status: 204 })
    }
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    if (pathname === '/api/todos/todo-1' && request.method() === 'PATCH') {
      const body = request.postDataJSON()
      if (body.text) todoText = body.text
      if (typeof body.completed === 'boolean') completed = body.completed
      return route.fulfill({ json: { id: 'todo-1', text: todoText, completed } })
    }
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })
  await page.goto('/')

  await page.getByRole('button', { name: '编辑' }).click()
  await page.locator('.todo-copy input').fill('整理并发送会议结论')
  await page.getByRole('button', { name: '保存' }).click()
  await expect(page.getByText('整理并发送会议结论')).toBeVisible()
  await page.locator('.todo-check').click()
  await expect(page.getByText('已完成 · 1')).toBeVisible()

  await page.getByRole('heading', { name: '周会重点复盘' }).click()
  await expect(page.getByRole('heading', { name: '核心结论' })).toBeVisible()

  await expect(page.getByRole('button', { name: '意见反馈' })).toHaveCount(0)

  await page.getByRole('button', { name: '关闭详情' }).click()
  await page.getByRole('button', { name: '清除所有历史' }).click()
  await page.getByRole('button', { name: '永久清除' }).click()
  await expect(page.getByRole('heading', { name: '先上传音频' })).toBeVisible()
})
