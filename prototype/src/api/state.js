const PROVIDER_NAMES = { kimi: 'Kimi', deepseek: 'DeepSeek', openai: 'OpenAI' }
const SCENE_LABELS = {
  meeting: '会议纪要',
  parenting: '家庭教育',
  content: '内容推荐',
  growth: '成长建议',
  inspiration: '闲聊灵感',
}


function timeLabel(value) {
  if (!value) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}


export function normalizeProviders(payload) {
  const providers = Object.fromEntries(['kimi', 'deepseek', 'openai'].map((id) => [id, {
    name: PROVIDER_NAMES[id],
    configured: false,
    state: 'initializing',
    active: false,
    lastChecked: '',
    error: '',
    errorCode: null,
    cooldownUntil: null,
  }]))
  for (const item of payload?.providers ?? []) {
    if (!providers[item.provider_id]) continue
    providers[item.provider_id] = {
      ...providers[item.provider_id],
      name: item.display_name ?? providers[item.provider_id].name,
      configured: ['available', 'unavailable', 'validating'].includes(item.state),
      state: item.state,
      active: Boolean(item.active),
      lastChecked: item.last_validated_at ? timeLabel(item.last_validated_at) : '',
      error: item.error_message ?? '',
      errorCode: item.error_code ?? null,
      cooldownUntil: item.cooldown_until ?? null,
    }
  }
  return {
    providers,
    activeProvider: Object.values(providers).find((item) => item.active)
      ? Object.entries(providers).find(([, item]) => item.active)[0]
      : 'kimi',
  }
}


function normalizeSection(section) {
  if (section.kind === 'text') return { title: section.title, content: section.text ?? '' }
  if (section.kind === 'grouped_items') {
    return {
      title: section.title,
      items: (section.groups ?? []).flatMap((group) => [group.title, ...group.items]),
    }
  }
  return { title: section.title, items: section.items ?? [] }
}


function normalizeConversation(messages = []) {
  const pairs = []
  for (const message of messages) {
    if (message.role === 'user') {
      pairs.push({ q: message.content, a: '' })
    } else if (message.role === 'assistant' && pairs.length) {
      pairs[pairs.length - 1].a = message.content
    }
  }
  return pairs.filter((pair) => pair.q && pair.a)
}


export function normalizeFeed(payload) {
  const batches = new Map()
  for (const day of payload?.days ?? []) {
    for (const item of day.cards ?? []) {
      if (!batches.has(item.batch_id)) {
        batches.set(item.batch_id, {
          id: item.batch_id,
          date: day.date,
          uploadedAt: timeLabel(item.uploaded_at),
          uploadedAtRaw: item.uploaded_at,
          audio: [],
          transcript: '',
          cards: [],
          qa: {},
        })
      }
      const shell = item.payload?.card ?? {}
      const batch = batches.get(item.batch_id)
      batch.cards.push({
        id: item.id,
        sceneId: item.scene_id,
        label: SCENE_LABELS[item.scene_id] ?? item.scene_id,
        title: shell.title ?? '未命名结果',
        summary: shell.summary ?? '',
        timeLabel: timeLabel(item.uploaded_at),
        meta: '查看 AI 分析详情',
        detailSections: (item.payload?.detail_sections ?? []).map(normalizeSection),
      })
      batch.qa[item.id] = normalizeConversation(item.qa)
    }
  }
  return {
    feed: [...batches.values()].sort((a, b) => b.uploadedAtRaw.localeCompare(a.uploadedAtRaw)),
    todos: (payload?.todos ?? []).map((item) => ({
      id: item.id,
      text: item.text,
      due: item.due_at ? new Date(item.due_at).toLocaleString('zh-CN') : '未设置截止时间',
      overdue: false,
      completed: Boolean(item.completed),
    })),
  }
}


function durationLabel(milliseconds) {
  if (!milliseconds) return '时长未知'
  const seconds = Math.round(milliseconds / 1000)
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return minutes ? `${minutes}分${String(rest).padStart(2, '0')}秒` : `${rest}秒`
}


export function normalizeHistory(payload) {
  return (payload?.days ?? []).map((day) => ({
    id: day.date,
    date: day.date,
    uploadedAt: timeLabel(day.audio?.[0]?.uploaded_at),
    files: (day.audio ?? []).map((item) => ({
      id: item.id,
      name: item.original_name,
      type: item.original_name.split('.').pop()?.toUpperCase() ?? 'AUDIO',
      size: '本地文件',
      duration: durationLabel(item.duration_ms),
      time: timeLabel(item.uploaded_at),
    })),
  }))
}


export function normalizePrompts(payload) {
  return Object.fromEntries((payload?.prompts ?? []).map((item) => [item.scene_id, {
    current: item.content,
    version: item.version,
  }]))
}
