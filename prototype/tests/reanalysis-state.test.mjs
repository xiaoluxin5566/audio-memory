import assert from 'node:assert/strict'
import test from 'node:test'

import { getReanalysisView, normalizeReanalysisPreview } from '../src/api/state.js'

test('no history disables reanalysis entry', () => {
  const view = getReanalysisView(null, { source_batch_count: 0, blockers: ['没有可重新分析的历史'] })
  assert.equal(view.state, 'disabled')
  assert.equal(view.buttonLabel, '重新分析历史')
  assert.equal(view.canClearHistory, true)
})

test('preview presents cost and frozen current model details', () => {
  const preview = normalizeReanalysisPreview({
    source_batch_count: 3,
    audio_file_count: 7,
    transcript_character_count: 12345,
    provider_display_name: 'Kimi', model_id: 'kimi-k2.5',
    prompt_summary: { todo: { version: 2 }, meeting: { version: 3 }, parenting: { version: 1 }, content: { version: 4 }, growth: { version: 5 }, inspiration: { version: 6 } },
    estimated_calls_min: 18, estimated_calls_max: 24, whisper_calls: 0, blockers: [], preview_token: 'preview-token',
  })
  assert.equal(preview.batchCount, 3)
  assert.equal(preview.fileCount, 7)
  assert.equal(preview.characterCount, 12345)
  assert.equal(preview.modelLabel, 'Kimi · kimi-k2.5')
  assert.equal(preview.promptVersions.length, 6)
  assert.equal(preview.callRange, '18–24 次')
  assert.match(preview.costNotice, /会调用当前模型并产生 API 费用/)
})

test('active, paused, stopped and partial batches have actionable display state', () => {
  const running = getReanalysisView({ id: 'r1', status: 'running', total: 18, pending: 12, running: 1, succeeded: 3, failed: 1, stopped: 1 })
  assert.equal(running.state, 'running')
  assert.equal(running.buttonLabel, '重新分析中 3/18')
  assert.equal(running.canClearHistory, false)
  assert.equal(running.counts.failed, 1)

  const paused = getReanalysisView({ id: 'r1', status: 'paused', total: 18, pending: 9, succeeded: 5, failed: 2, stopped: 2 })
  assert.equal(paused.state, 'paused')
  assert.equal(paused.actionLabel, '继续重新分析')
  assert.equal(paused.canClearHistory, false)

  const partial = getReanalysisView({ id: 'r1', status: 'completed_with_failures', total: 5, pending: 0, succeeded: 3, failed: 2, stopped: 0 })
  assert.match(partial.completionCopy, /已完成 3 次，2 次分析失败/)
  assert.equal(partial.canClearHistory, true)

  const allFailed = getReanalysisView({ id: 'r1', status: 'completed_with_failures', total: 5, pending: 0, succeeded: 0, failed: 5, stopped: 0 })
  assert.equal(allFailed.completionCopy, '重新分析失败，历史结果未发生变化')

  const stopped = getReanalysisView({ id: 'r1', status: 'stopped', total: 5, pending: 0, succeeded: 2, failed: 0, stopped: 3 })
  assert.equal(stopped.actionLabel, '继续剩余项目')
  assert.equal(stopped.canClearHistory, true)

  const profileFailed = getReanalysisView({ id: 'r1', status: 'content_completed_profile_failed', total: 5, pending: 0, succeeded: 5, failed: 0, stopped: 0 })
  assert.equal(profileFailed.actionLabel, '重试画像更新')
  assert.match(profileFailed.completionCopy, /个性化画像更新失败/)
})
