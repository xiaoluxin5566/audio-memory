import { DEFAULT_PROMPTS } from './defaults.js';

const CARD_ORDER = ['meeting', 'parenting', 'content', 'growth', 'inspiration'];

export function createInitialState() {
  const prompts = Object.fromEntries(Object.entries(DEFAULT_PROMPTS).map(([id, current]) => [
    id,
    { current, version: 1, versions: [] },
  ]));
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
    prompts,
    hiddenProfile: null,
  };
}

export function orderCards(cards) {
  return [...cards].sort((a, b) => CARD_ORDER.indexOf(a.sceneId) - CARD_ORDER.indexOf(b.sceneId));
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
