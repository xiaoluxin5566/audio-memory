import test from 'node:test'
import assert from 'node:assert/strict'

import { normalizeReportDocument } from '../src/reportDocument.js'


function validDocument() {
  return {
    schema_version: 1,
    title: '一天的三个判断',
    overview: {
      summary: '你需要把判断变成验证。',
      rows: [{ phase: '下午', event: '完成面试。', improvement: '确认岗位边界。', evidence_segment_ids: ['seg-1'] }],
    },
    sections: [{
      title: '职业选择',
      blocks: [
        { type: 'paragraph', text: '先确认事实。' },
        { type: 'source_quote', text: '我对这个岗位感兴趣', evidence_segment_ids: ['seg-1'] },
        { type: 'suggested_wording', text: '我想确认岗位目标。' },
        { type: 'table', columns: ['问题', '验证'], rows: [['岗位边界', '询问负责人']] },
        { type: 'subsection', title: '下一步', blocks: [{ type: 'numbered_list', items: ['整理问题', '发出确认'] }] },
      ],
    }],
    todos: [],
    evidence_segment_ids: ['seg-1'],
    external_source_ids: [],
  }
}


test('normalizes a complete version-one document without deriving presentation values', () => {
  const input = validDocument()
  const result = normalizeReportDocument(input)

  assert.deepEqual(result, input)
  assert.equal('number' in result.sections[0], false)
  assert.equal(result.sections[0].blocks[1].type, 'source_quote')
  assert.equal(result.sections[0].blocks[2].type, 'suggested_wording')
})


test('rejects unknown versions and malformed structural arrays', () => {
  const unknown = validDocument()
  unknown.schema_version = 2
  const badRows = validDocument()
  badRows.sections[0].blocks[3].rows = [['missing second cell']]
  const unknownBlock = validDocument()
  unknownBlock.sections[0].blocks[0] = { type: 'card', text: 'not supported' }

  assert.equal(normalizeReportDocument(unknown), null)
  assert.equal(normalizeReportDocument({ ...validDocument(), sections: null }), null)
  assert.equal(normalizeReportDocument(badRows), null)
  assert.equal(normalizeReportDocument(unknownBlock), null)
})


test('rejects partial documents instead of partially rendering them', () => {
  const missingOverviewRows = validDocument()
  delete missingOverviewRows.overview.rows
  const emptySections = validDocument()
  emptySections.sections = []

  assert.equal(normalizeReportDocument(missingOverviewRows), null)
  assert.equal(normalizeReportDocument(emptySections), null)
  assert.equal(normalizeReportDocument(null), null)
})
