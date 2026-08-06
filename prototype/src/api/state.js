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
    modelName: '',
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
      modelName: item.model_id ?? '',
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

function textList(items = []) {
  return items.map((item) => typeof item === 'string' ? item : item?.content).filter(Boolean)
}

function labelled(label, value) {
  return value ? `${label}：${value}` : ''
}

function detailBlock(title, { content = '', items = [], eventTitle = '' } = {}) {
  return { title, content, items: items.filter(Boolean), eventTitle }
}

function meetingBlocks(detail = {}) {
  const participants = (detail.participants ?? []).map((item) => [item.display_name, item.role].filter(Boolean).join(' · '))
  return [
    detailBlock('会议背景', { content: detail.background }),
    detailBlock('参与者', { items: participants }),
    detailBlock('核心结论', { items: textList(detail.core_conclusions) }),
    detailBlock('已确认决策', { items: textList(detail.decisions) }),
    detailBlock('待确认问题', { items: textList(detail.open_questions) }),
    detailBlock('会议待办', { items: (detail.meeting_todos ?? []).map((item) => item.text) }),
    detailBlock('讨论议题', { items: textList(detail.discussion_topics) }),
  ].filter((block) => block.content || block.items.length)
}

function parentingBlocks(detail = {}) {
  const blocks = [detailBlock('整体观察', { content: detail.overall_observation })]
  for (const interaction of detail.interactions ?? []) {
    const items = [
      ...(interaction.child_difficulties ?? []).map((item) => labelled('孩子的困难', item.content)),
      ...(interaction.emotional_signals ?? []).map((item) => labelled('情绪信号', item.signal)),
      ...(interaction.observed_parent_actions ?? []).map((item) => labelled('观察到的做法', item.content)),
      ...(interaction.possible_issues ?? []).map((item) => labelled('可能的问题', item.content)),
      ...(interaction.recommendations ?? []).flatMap((item) => [
        labelled('建议', item.title), item.why_it_helps, ...(item.steps ?? []), item.suggested_language,
      ]),
    ]
    blocks.push(detailBlock(interaction.title || '一次亲子互动', { content: interaction.background, items, eventTitle: interaction.title || '一次亲子互动' }))
  }
  return blocks.filter((block) => block.content || block.items.length)
}

function contentBlocks(detail = {}) {
  const blocks = []
  for (const item of detail.consumed_items ?? []) {
    blocks.push(detailBlock(item.display_title || '一段内容', {
      content: item.introduction,
      items: [
        item.platform && `平台：${item.platform}`,
        ...textList(item.key_points),
        ...textList(item.user_reactions),
      ],
      eventTitle: item.display_title || '一段内容',
    }))
  }
  if (detail.cross_event_insights?.length) blocks.push(detailBlock('跨内容发现', { items: textList(detail.cross_event_insights) }))
  if (detail.recommendations?.length) blocks.push(detailBlock('更贴合你的内容', { items: detail.recommendations.flatMap((item) => [
    [item.title, item.creator].filter(Boolean).join(' · '), item.introduction, item.recommendation_reason, item.search_query && `搜索：${item.search_query}`,
  ]) }))
  return blocks.filter((block) => block.content || block.items.length)
}

function growthBlocks(detail = {}) {
  const blocks = [detailBlock('整体评估', { content: detail.overall_assessment })]
  for (const direction of detail.directions ?? []) {
    blocks.push(detailBlock(direction.title || '成长方向', { content: direction.pattern_summary, items: [direction.importance] }))
    for (const item of direction.cases ?? []) {
      blocks.push(detailBlock(`${direction.title || '成长方向'} · ${item.title || '具体场景'}`, {
        content: item.scene,
        items: [item.observed_behavior, item.counterparty_response, item.problem, item.reasoning],
        eventTitle: item.title || direction.title || '具体场景',
      }))
    }
    const recommendation = direction.recommendation
    if (recommendation) blocks.push(detailBlock(`${direction.title || '成长方向'} · 建议`, { items: [recommendation.goal, recommendation.method, ...(recommendation.steps ?? []), recommendation.suggested_language, recommendation.practice_task, recommendation.success_signal] }))
    if (direction.resources?.length) blocks.push(detailBlock(`${direction.title || '成长方向'} · 延伸资源`, { items: direction.resources.flatMap((item) => [[item.title, item.creator].filter(Boolean).join(' · '), item.reason, item.search_query && `搜索：${item.search_query}`]) }))
  }
  if (detail.strengths_to_keep?.length) blocks.push(detailBlock('值得保持的优势', { items: textList(detail.strengths_to_keep) }))
  return blocks.filter((block) => block.content || block.items.length)
}

function inspirationBlocks(detail = {}) {
  const blocks = [detailBlock('整体价值', { content: detail.overall_value })]
  for (const idea of detail.ideas ?? []) {
    blocks.push(detailBlock(idea.title || '一个灵感', {
      content: idea.background,
      items: [idea.conversation_summary, idea.core_idea, idea.why_valuable, idea.novelty_basis, ...(idea.next_steps ?? []).flatMap((step) => [step.direction, step.action])],
      eventTitle: idea.title || '一个灵感',
    }))
  }
  if (detail.connections?.length) blocks.push(detailBlock('灵感之间的联系', { items: textList(detail.connections) }))
  return blocks.filter((block) => block.content || block.items.length)
}

function strictCardDetails(sceneId, detail = {}) {
  if (sceneId === 'content') return { consumedItems: (detail.consumed_items ?? []).map((item) => ({
    title: item.display_title, introduction: item.introduction, keyPoints: textList(item.key_points), reactions: textList(item.user_reactions),
  })) }
  if (sceneId === 'parenting') return { interactions: (detail.interactions ?? []).map((item) => ({ title: item.title, background: item.background })) }
  if (sceneId === 'growth') return { directions: (detail.directions ?? []).map((item) => ({ title: item.title })) }
  if (sceneId === 'inspiration') return { ideas: (detail.ideas ?? []).map((item) => ({ title: item.title, background: item.background })) }
  return {}
}

function strictBlocks(sceneId, detail) {
  if (sceneId === 'meeting') return meetingBlocks(detail)
  if (sceneId === 'parenting') return parentingBlocks(detail)
  if (sceneId === 'content') return contentBlocks(detail)
  if (sceneId === 'growth') return growthBlocks(detail)
  if (sceneId === 'inspiration') return inspirationBlocks(detail)
  return []
}

function normalizeStrictCards(item, batch) {
  const payload = item.payload
  if (!payload?.scene_id || !Array.isArray(payload.cards)) return null
  return payload.cards.map((source, index) => {
    const shell = source.card ?? {}
    const detail = source.detail ?? {}
    const topic = payload.scene_id === 'meeting' ? detail.topic : ''
    return {
      id: `${item.id}:${index}`,
      apiId: item.id,
      sceneId: item.scene_id,
      label: SCENE_LABELS[item.scene_id] ?? item.scene_id,
      title: shell.title || topic || '未命名结果',
      summary: shell.summary ?? '',
      timeLabel: timeLabel(item.uploaded_at),
      meta: '查看 AI 分析详情',
      detailSections: strictBlocks(item.scene_id, detail),
      details: strictCardDetails(item.scene_id, detail),
    }
  })
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
      const batch = batches.get(item.batch_id)
      const strictCards = normalizeStrictCards(item, batch)
      if (strictCards) {
        batch.cards.push(...strictCards)
        for (const card of strictCards) batch.qa[card.id] = []
      } else {
        const shell = item.payload?.card ?? {}
        batch.cards.push({
          id: item.id,
          apiId: item.id,
          sceneId: item.scene_id,
          label: SCENE_LABELS[item.scene_id] ?? item.scene_id,
          title: shell.title ?? '未命名结果',
          summary: shell.summary ?? '',
          timeLabel: timeLabel(item.uploaded_at),
          meta: '查看 AI 分析详情',
          detailSections: (item.payload?.detail_sections ?? []).map(normalizeSection),
          details: {},
        })
        batch.qa[item.id] = normalizeConversation(item.qa)
      }
    }
  }
  return {
    feed: [...batches.values()].sort((a, b) => b.uploadedAtRaw.localeCompare(a.uploadedAtRaw)),
    todos: (payload?.todos ?? []).map((item) => ({
      id: item.id,
      text: item.text,
      dueAt: item.due_at ?? '',
      due: item.due_at ? new Date(item.due_at).toLocaleString('zh-CN') : '未设置截止时间',
      overdue: Boolean(item.overdue),
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

export function normalizeReanalysisPreview(payload = {}) {
  const prompts = Object.entries(payload.prompt_summary ?? {}).map(([sceneId, summary]) => ({
    sceneId,
    version: summary.version,
    label: `${SCENE_LABELS[sceneId] ?? (sceneId === 'todo' ? '待办事项' : sceneId)} v${summary.version}`,
  }))
  return {
    batchCount: payload.source_batch_count ?? 0,
    fileCount: payload.audio_file_count ?? 0,
    characterCount: payload.transcript_character_count ?? 0,
    modelLabel: [payload.provider_display_name, payload.model_id].filter(Boolean).join(' · '),
    promptVersions: prompts,
    callRange: `${payload.estimated_calls_min ?? 0}–${payload.estimated_calls_max ?? 0} 次`,
    blockers: payload.blockers ?? [],
    previewToken: payload.preview_token ?? '',
    costNotice: '本次会调用当前模型并产生 API 费用；确认后不会重新转写音频。',
  }
}

const ACTIVE_REANALYSIS_STATES = new Set(['pending', 'running', 'paused', 'stopping'])

export function isActiveReanalysis(batch) {
  return ACTIVE_REANALYSIS_STATES.has(batch?.status)
}

export function getReanalysisView(batch, preview) {
  const counts = { pending: batch?.pending ?? 0, running: batch?.running ?? 0, succeeded: batch?.succeeded ?? 0, failed: batch?.failed ?? 0, stopped: batch?.stopped ?? 0, total: batch?.total ?? 0 }
  if (!batch) {
    const disabled = !preview || (preview.source_batch_count ?? preview.batchCount ?? 0) === 0 || (preview.blockers?.length ?? 0) > 0
    return { state: disabled ? 'disabled' : 'idle', buttonLabel: '重新分析历史', actionLabel: '确认重新分析', canClearHistory: true, counts, completionCopy: '' }
  }
  const base = { counts, canClearHistory: !isActiveReanalysis(batch), batch }
  if (batch.status === 'running' || batch.status === 'pending') return { ...base, state: 'running', buttonLabel: `重新分析中 ${counts.succeeded}/${counts.total}`, actionLabel: '停止重新分析', completionCopy: '' }
  if (batch.status === 'paused') return { ...base, state: 'paused', buttonLabel: '重新分析已暂停', actionLabel: '继续重新分析', completionCopy: '' }
  if (batch.status === 'stopping') return { ...base, state: 'stopping', buttonLabel: '正在停止重新分析', actionLabel: '', completionCopy: '' }
  if (batch.status === 'completed_with_failures') return { ...base, state: 'finished', buttonLabel: '重新分析历史', actionLabel: '重新分析历史', completionCopy: `已完成 ${counts.succeeded} 次，${counts.failed} 次分析失败，失败项旧结果已保留` }
  if (batch.status === 'content_completed_profile_failed') return { ...base, state: 'finished', buttonLabel: '重新分析历史', actionLabel: '重试画像更新', completionCopy: '历史内容已更新，个性化画像更新失败，可重新尝试' }
  if (batch.status === 'stopped') return { ...base, state: 'finished', buttonLabel: '重新分析历史', actionLabel: '重新分析历史', completionCopy: `已停止；已完成 ${counts.succeeded} 次，剩余 ${counts.pending + counts.stopped} 次未处理` }
  return { ...base, state: 'finished', buttonLabel: '重新分析历史', actionLabel: '重新分析历史', completionCopy: `已用最新 Prompt 重新分析 ${counts.succeeded} 次历史上传` }
}
