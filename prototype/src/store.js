const CARD_ORDER = ['meeting', 'parenting', 'content', 'growth', 'inspiration'];

export function createInitialState() {
  return {
    version: 1,
    providers: {
      kimi: { name: 'Kimi', modelName: '', configured: false, lastChecked: '' },
      deepseek: { name: 'DeepSeek', modelName: '', configured: false, lastChecked: '' },
      openai: { name: 'OpenAI', modelName: '', configured: false, lastChecked: '' },
    },
    activeProvider: 'kimi',
    upload: { files: [], error: '', paused: false },
    job: null,
    feed: [],
    todos: [],
    history: [],
    prompts: {},
    hiddenProfile: null,
  };
}

export function orderCards(cards) {
  return [...cards].sort((a, b) => CARD_ORDER.indexOf(a.sceneId) - CARD_ORDER.indexOf(b.sceneId));
}

export function uploadFailureState(error) {
  const unsupported = error?.code === 'unsupported_format';
  return { invalid: unsupported, failed: !unsupported, paused: unsupported };
}

export function canRemoveUploadFile(job, _file, starting = false) {
  return !starting && (!job || job.stage === 'uploading');
}

export function uploadRemovalBlockMessage(job, starting = false) {
  return canRemoveUploadFile(job, null, starting)
    ? ''
    : '任务进行中，不能删除音频文件';
}

export function jobProgressValue(job) {
  const live = job?.live_progress_percent;
  const durable = job?.progress_percent;
  const source = live == null ? durable : live;
  const value = Number(source);
  return Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
}

export function jobModelDisplayName(job) {
  const providerId = String(job?.provider_id ?? '').toLowerCase();
  const modelId = String(job?.model_id ?? '').trim();
  if (providerId === 'glm') return modelId.replace(/^glm-/i, 'GLM ');
  if (providerId === 'deepseek') {
    return modelId.replace(/^deepseek-/i, 'DeepSeek ').replace(/-/g, ' ')
      .replace(/\bv(\d+)\b/i, 'V$1').replace(/\bpro\b/i, 'Pro');
  }
  if (providerId === 'kimi') return modelId.replace(/^kimi-/i, 'Kimi ').toUpperCase().replace(/^KIMI /, 'Kimi ');
  if (providerId === 'openai') return modelId.replace(/^gpt-/i, 'GPT-');
  return modelId || job?.provider_id || '模型';
}

export function analysisProgressCopy(job) {
  if (job?.analysis_phase === 'pending') {
    return {
      title: '等待分析线程开始',
      detail: '转写已安全保存，任务正在队列中等待领取。',
      failed: false,
    };
  }
  if (job?.analysis_phase === 'running') {
    const phases = {
      generating: {
        title: `${jobModelDisplayName(job)} 正在生成报告初稿`,
        detail: '转写已安全保存，正在整理全文要点。',
      },
      auditing: {
        title: `${jobModelDisplayName(job)} 正在审校报告`,
        detail: '初稿已安全保存，正在检查完整性和证据。',
      },
      revising: {
        title: `${jobModelDisplayName(job)} 正在修订报告`,
        detail: '审校已完成，正在修正需要改进的内容。',
      },
      publishing: {
        title: '正在发布报告',
        detail: '分析已完成，正在将最终报告安全写入首页。',
      },
    };
    const current = phases[job.analysis_detail_phase];
    if (current) return { ...current, failed: false };
    return {
      title: `${jobModelDisplayName(job)} 正在阅读全文并生成报告`,
      detail: '报告正在安全发布，完成前请保持应用运行。',
      failed: false,
    };
  }
  return {
    title: '分析未开始，可重试',
    detail: '完整转写已保留，重试不会再次执行 Whisper。',
    failed: true,
  };
}

export function getFeedbackFormState(rating, comment = '') {
  const showDetails = rating === 'inaccurate';
  return {
    showDetails,
    canSubmit: rating === 'accurate' || (showDetails && Boolean(comment.trim())),
  };
}

export function formatJobEta(job) {
  if (job.stage === 'analyzing') return '正在生成分析结果…';
  if (job.stage === 'ready_to_commit') return '正在安全发布报告…';
  if (job.transcription_mode === 'cloud') {
    return '云端任务处理中，进度按已保存阶段更新';
  }
  if (job.eta_state !== 'ready' || job.eta_seconds == null) {
    return '正在估算剩余时间…';
  }
  if (job.eta_seconds < 60) return '预计不到 1 分钟';
  return `预计还需约 ${Math.ceil(job.eta_seconds / 60)} 分钟`;
}

export function jobFailureCopy(job) {
  if (job.error_code === 'cloud_asr_failed') {
    return {
      title: '云端转写未完成',
      body: '音频仍安全保存在本机，可从失败位置继续；尚未生成完整转写和报告。',
      action: '继续云端转写',
    };
  }
  if (job.error_code === 'report_audit_pending') {
    return {
      title: '报告已生成，审计待重试',
      body: '已保留报告初稿和已完成的审计块，重试不会重新转写。',
      action: '继续审计',
    };
  }
  return {
    title: '模型分析失败',
    body: '已保留完整转写；可修改当前厂商后重新分析，不会再次执行 Whisper。',
    action: '重新分析',
  };
}


export function jobRecoveryAction(job) {
  if (job?.stage === 'failed' && job?.error_code === 'cloud_asr_failed') {
    return 'resume-cloud-asr';
  }
  if (job?.stage === 'failed' || job?.analysis_phase === 'failed') {
    return 'retry-analysis';
  }
  if (job?.stage === 'interrupted') return 'resume-transcription';
  return null;
}
