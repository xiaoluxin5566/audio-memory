import assert from 'node:assert/strict'
import test from 'node:test'

import { analysisProgressCopy } from '../src/store.js'


test('pending analysis does not claim the provider is already reading', () => {
  assert.deepEqual(analysisProgressCopy({
    stage: 'analyzing',
    analysis_phase: 'pending',
    provider_id: 'deepseek',
    model_id: 'deepseek-v4-pro',
  }), {
    title: '等待分析线程开始',
    detail: '转写已安全保存，任务正在队列中等待领取。',
    failed: false,
  })
})


test('provider reading copy is shown only after a worker claim', () => {
  assert.deepEqual(analysisProgressCopy({
    stage: 'analyzing',
    analysis_phase: 'running',
    provider_id: 'deepseek',
    model_id: 'deepseek-v4-pro',
  }), {
    title: 'DeepSeek V4 Pro 正在阅读全文并生成报告',
    detail: '报告正在安全发布，完成前请保持应用运行。',
    failed: false,
  })
})


test('missing durable queue is presented as retryable instead of running', () => {
  assert.deepEqual(analysisProgressCopy({
    stage: 'analyzing',
    analysis_phase: 'failed',
    provider_id: 'deepseek',
    model_id: 'deepseek-v4-pro',
  }), {
    title: '分析未开始，可重试',
    detail: '完整转写已保留，重试不会再次执行 Whisper。',
    failed: true,
  })
})
