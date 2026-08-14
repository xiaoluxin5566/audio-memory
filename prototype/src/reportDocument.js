const LEAF_TYPES = new Set([
  'paragraph', 'source_quote', 'suggested_wording', 'subheading', 'quote', 'bullet_list', 'numbered_list', 'table',
])

function isText(value) {
  return typeof value === 'string' && value.trim().length > 0
}

function textArray(value) {
  return Array.isArray(value) && value.length > 0 && value.every(isText)
}

function idArray(value, { required = false } = {}) {
  return Array.isArray(value) && (!required || value.length > 0) && value.every(isText)
}

function normalizeLeaf(block) {
  if (!block || typeof block !== 'object' || !LEAF_TYPES.has(block.type)) return null
  if (['paragraph', 'suggested_wording'].includes(block.type) && isText(block.text)) {
    return { type: block.type, text: block.text }
  }
  if (block.type === 'subheading' && isText(block.title)) {
    return { type: block.type, title: block.title }
  }
  if (block.type === 'quote' && isText(block.text) && idArray(block.evidence_segment_ids, { required: true })) {
    return { type: block.type, text: block.text, evidence_segment_ids: [...block.evidence_segment_ids] }
  }
  if (block.type === 'source_quote' && isText(block.text) && idArray(block.evidence_segment_ids, { required: true })) {
    return { type: block.type, text: block.text, evidence_segment_ids: [...block.evidence_segment_ids] }
  }
  if (['bullet_list', 'numbered_list'].includes(block.type) && textArray(block.items)) {
    return { type: block.type, items: [...block.items] }
  }
  if (block.type === 'table' && textArray(block.columns) && block.columns.length >= 2
    && Array.isArray(block.rows) && block.rows.length > 0
    && block.rows.every((row) => Array.isArray(row) && row.length === block.columns.length && row.every(isText))) {
    return { type: block.type, columns: [...block.columns], rows: block.rows.map((row) => [...row]) }
  }
  return null
}

function normalizeBlock(block) {
  if (block?.type !== 'subsection') return normalizeLeaf(block)
  if (!isText(block.title) || !Array.isArray(block.blocks) || block.blocks.length === 0) return null
  const blocks = block.blocks.map(normalizeLeaf)
  if (blocks.some((item) => item === null)) return null
  return { type: 'subsection', title: block.title, blocks }
}

export function normalizeReportDocument(value) {
  if (!value || typeof value !== 'object' || value.schema_version !== 1) return null
  if (!isText(value.title) || !isText(value.overview?.summary)) return null
  if (!Array.isArray(value.overview.rows) || value.overview.rows.length === 0) return null
  const rows = value.overview.rows.map((row) => {
    if (!isText(row?.phase) || !isText(row?.event) || !isText(row?.improvement)
      || !idArray(row?.evidence_segment_ids, { required: true })) return null
    return { phase: row.phase, event: row.event, improvement: row.improvement, evidence_segment_ids: [...row.evidence_segment_ids] }
  })
  if (rows.some((row) => row === null) || !Array.isArray(value.sections) || value.sections.length === 0) return null
  const sections = value.sections.map((section) => {
    if (!isText(section?.title)) return null
    if (isText(section.content) && !section.blocks) {
      return { title: section.title, content: section.content, evidence_segment_ids: idArray(section.evidence_segment_ids) ? [...section.evidence_segment_ids] : [] }
    }
    if (!Array.isArray(section.blocks) || section.blocks.length === 0) return null
    const blocks = section.blocks.map(normalizeBlock)
    if (blocks.some((block) => block === null)) return null
    return { title: section.title, blocks }
  })
  if (sections.some((section) => section === null)
    || !Array.isArray(value.todos)
    || !idArray(value.evidence_segment_ids)
    || !idArray(value.external_source_ids)) return null
  return {
    schema_version: 1,
    title: value.title,
    overview: { summary: value.overview.summary, rows },
    sections,
    todos: value.todos.map((todo) => ({ ...todo })),
    evidence_segment_ids: [...value.evidence_segment_ids],
    external_source_ids: [...value.external_source_ids],
  }
}
