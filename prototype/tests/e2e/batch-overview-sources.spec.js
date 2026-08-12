import { expect, test } from '@playwright/test'

test('batch overview is a distinct entry point and card sources stay separate from recording evidence', async ({ page }) => {
  await page.route(/^http:\/\/127\.0\.0\.1:4173\/api\//, async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'test-session' } })
    if (pathname === '/api/providers') return route.fulfill({ json: { providers: [{ provider_id: 'deepseek', display_name: 'DeepSeek', state: 'available', active: true }] } })
    if (pathname === '/api/feed') return route.fulfill({ json: { todos: [], days: [{
      date: '2026年8月12日',
      cards: [
        {
          id: 'overview-1', batch_id: 'batch-1', scene_id: 'batch_overview', uploaded_at: '2026-08-12T10:00:00Z', qa: [],
          payload: { scene_id: 'batch_overview', kind: 'batch_overview', overview: { title: '本次概览', summary: '会议和行业研究交替出现。', scene_ids: ['meeting', 'research'] } },
        },
        {
          id: 'meeting-1', batch_id: 'batch-1', scene_id: 'meeting', uploaded_at: '2026-08-12T10:00:00Z', qa: [],
          evidence: [{ card_index: 0, segments: [{ segment_id: 'seg_0_1', start_ms: 2_000, end_ms: 5_000, playback_url: '/api/cards/meeting-1/evidence/seg_0_1/audio' }] }],
          payload: { scene_id: 'meeting', cards: [{ card: { title: '行业研究的启发', summary: '将外部研究与录音观点对照。' }, detail: {}, external_source_ids: ['source-1'] }] },
          sources: [{ source_id: 'source-1', title: 'Example Research', url: 'https://example.org/research' }],
        },
      ],
    }] } })
    if (pathname === '/api/history') return route.fulfill({ json: { days: [] } })
    if (pathname === '/api/prompts') return route.fulfill({ json: { prompts: [] } })
    if (pathname === '/api/jobs/active') return route.fulfill({ json: null })
    return route.fulfill({ status: 404, json: { detail: 'not found' } })
  })

  await page.goto('/')

  await expect(page.locator('.batch-overview')).toContainText('本次概览')
  await expect(page.locator('.batch-overview')).toContainText('会议和行业研究交替出现。')
  await expect(page.locator('.result-card')).toHaveCount(1)

  await page.getByRole('heading', { name: '行业研究的启发' }).click()
  await expect(page.locator('.external-sources')).toContainText('外部资料')
  await expect(page.locator('.external-sources')).toContainText('Example Research')
  await expect(page.locator('.external-sources')).toContainText('example.org')
  await expect(page.locator('.external-sources a')).toHaveAttribute('href', 'https://example.org/research')
  await expect(page.locator('.evidence-playback')).toContainText('回听证据')
  await expect(page.locator('.evidence-playback .external-sources')).toHaveCount(0)
})
