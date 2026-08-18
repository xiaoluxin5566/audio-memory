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

export function jobProgressValue(job) {
  const live = job?.live_progress_percent;
  const durable = job?.progress_percent;
  const source = live == null ? durable : live;
  const value = Number(source);
  return Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
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
  if (job.eta_state !== 'ready' || job.eta_seconds == null) {
    return '正在估算剩余时间…';
  }
  if (job.eta_seconds < 60) return '预计不到 1 分钟';
  return `预计还需约 ${Math.ceil(job.eta_seconds / 60)} 分钟`;
}

export function jobFailureCopy(job) {
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
