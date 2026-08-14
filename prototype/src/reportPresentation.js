function cleanInline(value = '') {
  return String(value)
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/[`“”]/g, '')
    .trim()
}

function splitSections(markdown = '') {
  const lines = String(markdown).replace(/\r\n/g, '\n').split('\n')
  const sections = []
  let current = null
  let subsection = null

  for (const rawLine of lines) {
    const line = rawLine.trim()
    const h2 = /^##\s+(.+)$/.exec(line)
    const h3 = /^###\s+(.+)$/.exec(line)
    if (h2) {
      current = { title: cleanInline(h2[1]), lines: [], subsections: [] }
      sections.push(current)
      subsection = null
      continue
    }
    if (h3 && current) {
      subsection = { title: cleanInline(h3[1]), lines: [] }
      current.subsections.push(subsection)
      continue
    }
    if (!current) continue
    current.lines.push(line)
    if (subsection) subsection.lines.push(line)
  }
  return sections
}

function firstParagraph(lines = []) {
  const paragraph = []
  for (const line of lines) {
    if (!line) {
      if (paragraph.length) break
      continue
    }
    if (/^(?:[-*]|\d+[.)]|>|\|)/.test(line)) {
      if (paragraph.length) break
      continue
    }
    paragraph.push(cleanInline(line))
  }
  return paragraph.join(' ')
}

function numberedItems(lines = []) {
  return lines.flatMap((line) => {
    const match = /^\d+[.)]\s+(.+)$/.exec(line)
    return match ? [cleanInline(match[1])] : []
  })
}

function eventKind(text = '') {
  if (/(数学辅导|讲数学题|孩子|亲子)/.test(text)) return 'parenting'
  if (/(电话面试|岗位面试|学练机)/.test(text)) return 'interview'
  if (/(午餐|职业选择|组织问题|离职)/.test(text)) return 'career'
  return null
}

function improvementFor(kind, sections) {
  const sectionPatterns = {
    career: /(工作与职业选择|职业选择)/,
    interview: /(关键面试|岗位面试)/,
    parenting: /(亲子辅导|亲子教育)/,
  }
  const subsectionPatterns = {
    career: /(真正需要做|评分|选择标准|机会打分)/,
    interview: /(闭环|确认|后续)/,
    parenting: /(换一种讲法|建议|改进)/,
  }
  const section = sections.find((item) => sectionPatterns[kind].test(item.title))
  if (!section) return null
  const subsection = section.subsections.find((item) => subsectionPatterns[kind].test(item.title))
  const title = kind === 'career'
    ? cleanInline(section.title.split(/[：:]/).slice(1).join('：') || subsection?.title || section.title)
    : cleanInline(subsection?.title || section.title.split(/[：:]/).slice(1).join('：') || section.title)
  const detail = firstParagraph(subsection?.lines || section.lines)
  return title ? { title, detail } : null
}

export function buildReportEventMap(markdown = '') {
  const title = cleanInline(String(markdown).split('\n').find((line) => /^#\s+/.test(line))?.replace(/^#\s+/, '') || '')
  const sections = splitSections(markdown)
  const core = sections.find((section) => /核心结论/.test(section.title))
  const timeline = sections.find((section) => /重要时间轴/.test(section.title))
  if (!title || !core || !timeline) return null

  const candidates = numberedItems(timeline.lines).flatMap((item) => {
    const kind = eventKind(item)
    if (!kind) return []
    const improvement = improvementFor(kind, sections)
    if (!improvement) return []
    const parts = item.split(/[：:]/)
    const phase = cleanInline(parts.shift())
    const event = cleanInline(parts.join('：') || item)
    return [{ kind, phase, event, improvementTitle: improvement.title, improvementDetail: improvement.detail }]
  })
  const preferredEvent = {
    career: /(午餐中段|职业选择|组织问题)/,
    interview: /(午后|电话面试|岗位面试)/,
    parenting: /(晚间|数学辅导|讲数学题)/,
  }
  const events = ['career', 'interview', 'parenting'].flatMap((kind) => {
    const matches = candidates.filter((event) => event.kind === kind)
    return [matches.find((event) => preferredEvent[kind].test(`${event.phase} ${event.event}`)) || matches[0]].filter(Boolean)
  })

  if (events.length < 3) return null
  return {
    title,
    summary: firstParagraph(core.lines),
    events,
  }
}
