import test from 'node:test'
import assert from 'node:assert/strict'

import { canRemoveUploadFile, createInitialState, formatJobEta, getFeedbackFormState, jobFailureCopy, jobModelDisplayName, jobProgressValue, jobRecoveryAction, orderCards, uploadFailureState, uploadRemovalBlockMessage } from '../src/store.js'


test('only uploading jobs allow individual audio removal', () => {
  const file = { id: 'file-1', invalid: false, failed: false }

  assert.equal(canRemoveUploadFile({ stage: 'uploading' }, file), true)
  assert.equal(canRemoveUploadFile({ stage: 'uploading' }, file, true), false)
  for (const stage of ['transcribing', 'analyzing', 'ready_to_commit', 'interrupted', 'failed']) {
    assert.equal(canRemoveUploadFile({ stage }, file), false)
  }
})

test('locked audio removal keeps the button actionable and explains the block', () => {
  assert.equal(uploadRemovalBlockMessage({ stage: 'uploading' }, false), '')
  assert.equal(
    uploadRemovalBlockMessage({ stage: 'transcribing' }, false),
    '任务进行中，不能删除音频文件',
  )
  assert.equal(
    uploadRemovalBlockMessage({ stage: 'uploading' }, true),
    '任务进行中，不能删除音频文件',
  )
})


test('initial state waits for the runtime autonomous prompt list', () => {
  const state = createInitialState()
  assert.deepEqual(Object.keys(state.prompts), [])
  assert.deepEqual(state.feed, [])
  assert.deepEqual(state.history, [])
})


test('only an unsupported-format response labels an upload as unsupported', () => {
  assert.deepEqual(uploadFailureState({ code: 'unsupported_format' }), {
    invalid: true, failed: false, paused: true,
  })
  assert.deepEqual(uploadFailureState({ code: 'internal_error' }), {
    invalid: false, failed: true, paused: false,
  })
})


test('cards follow approved batch order', () => {
  const cards = ['inspiration', 'meeting', 'growth', 'parenting', 'content']
    .map((sceneId) => ({ sceneId }))
  assert.deepEqual(orderCards(cards).map((item) => item.sceneId), [
    'meeting', 'parenting', 'content', 'growth', 'inspiration',
  ])
})


test('feedback details are required only for inaccurate ratings', () => {
  assert.deepEqual(getFeedbackFormState(''), { showDetails: false, canSubmit: false })
  assert.deepEqual(getFeedbackFormState('accurate'), { showDetails: false, canSubmit: true })
  assert.deepEqual(getFeedbackFormState('inaccurate', '   '), { showDetails: true, canSubmit: false })
  assert.deepEqual(getFeedbackFormState('inaccurate', '会议结论遗漏了预算限制'), {
    showDetails: true,
    canSubmit: true,
  })
})


test('job ETA copy follows transcription and analysis states', () => {
  assert.equal(formatJobEta({ stage: 'transcribing', eta_state: 'estimating' }), '正在估算剩余时间…')
  assert.equal(formatJobEta({ stage: 'transcribing', eta_state: 'ready', eta_seconds: 59 }), '预计不到 1 分钟')
  assert.equal(formatJobEta({ stage: 'transcribing', eta_state: 'ready', eta_seconds: 901 }), '预计还需约 16 分钟')
  assert.equal(formatJobEta({ stage: 'analyzing' }), '正在生成分析结果…')
  assert.equal(formatJobEta({ stage: 'ready_to_commit' }), '正在安全发布报告…')
})


test('report audit failure is presented as generated and retryable', () => {
  assert.deepEqual(jobFailureCopy({ error_code: 'report_audit_pending' }), {
    title: '报告已生成，审计待重试',
    body: '已保留报告初稿和已完成的审计块，重试不会重新转写。',
    action: '继续审计',
  })
})

test('model analysis retry copy refers to cloud transcription', () => {
  assert.deepEqual(jobFailureCopy({ error_code: 'model_analysis_failed' }), {
    title: '模型分析失败',
    body: '已保留完整转写；可修改当前厂商后重新分析，不会再次执行云端转写。',
    action: '重新分析',
  })
})

test('cloud ASR failure never claims transcription completed', () => {
  assert.deepEqual(jobFailureCopy({ error_code: 'cloud_asr_failed' }), {
    title: '云端转写未完成',
    body: '音频仍安全保存在本机，可从失败位置继续；尚未生成完整转写和报告。',
    action: '继续云端转写',
  })
  assert.equal(jobRecoveryAction({ stage: 'failed', error_code: 'cloud_asr_failed' }), 'resume-cloud-asr')
})

test('managed storage preparation failure is routed back to cloud transcription', () => {
  assert.deepEqual(jobFailureCopy({ error_code: 'managed_storage_unavailable' }), {
    title: '云端转写准备失败',
    body: '音频仍安全保存在本机；产品正在重新建立临时存储授权，尚未生成完整转写和报告。',
    action: '继续云端转写',
  })
  assert.equal(jobRecoveryAction({ stage: 'failed', error_code: 'managed_storage_unavailable' }), 'resume-cloud-asr')
})

test('failed analysis detail retries analysis even when the top-level stage is stale', () => {
  assert.equal(jobRecoveryAction({ stage: 'failed', analysis_phase: 'failed' }), 'retry-analysis')
  assert.equal(jobRecoveryAction({ stage: 'analyzing', analysis_phase: 'failed' }), 'retry-analysis')
  assert.equal(jobRecoveryAction({ stage: 'interrupted', analysis_phase: null }), 'resume-transcription')
})

test('job progress prefers bounded live progress and preserves durable fallback', () => {
  assert.equal(jobProgressValue({ progress_percent: 63 }), 63)
  assert.equal(jobProgressValue({ progress_percent: 63, live_progress_percent: 66.375 }), 66.375)
  assert.equal(jobProgressValue({ progress_percent: 63, live_progress_percent: 120 }), 100)
})

test('analysis heading uses the model snapshot from the job', () => {
  assert.equal(jobModelDisplayName({ provider_id: 'glm', model_id: 'glm-5.2' }), 'GLM 5.2')
  assert.equal(jobModelDisplayName({ provider_id: 'deepseek', model_id: 'deepseek-v4-pro' }), 'DeepSeek V4 Pro')
})
