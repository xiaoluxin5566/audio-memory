import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { buildSync } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { normalizeFeed } from '../src/api/state.js';
import { ASSESSMENT_DIMENSIONS, normalizeCardAssessment } from '../src/cardAssessment.js';

const built = buildSync({
  entryPoints: [fileURLToPath(new URL('../src/components/CardAssessment.jsx', import.meta.url))],
  bundle: true, format: 'esm', platform: 'node', write: false, loader: { '.css': 'empty' },
});
const { CardAssessment, WritingDraftStatus } = await import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);
const render = (component, assessment) => renderToStaticMarkup(createElement(component, { assessment }));

function assessment(overrides = {}) {
  return {
    status: 'scored',
    dimensions: { factual_accuracy: 15, important_coverage: 15, analysis_depth: 21.25, actionability: 26, expression_structure: 15 },
    raw_total: 92.25, capped_reference_total: null, acceptance_total: 92.25, passed: true,
    deductions: [{ dimension: 'analysis_depth', rule: 'D3', points: 3.75, body_locator: '方案取舍', reason: '未交代关键取舍', evidence: ['三万元预算', '交付期限'], missing_content: '需要比较预算限制与交付期限如何改变两个方案的优先级' }],
    strengths: '保留了预算与期限', weaknesses: '缺少取舍分析', read_scope: ['会议完整原文'],
    ...overrides,
  };
}

function row(payload = {}) {
  return { id: 'row-1', batch_id: 'batch-1', scene_id: 'work_communication', uploaded_at: '2026-09-09T12:00:00Z', payload: {
    scene_id: 'work_communication', cards: [{ title: '预算方案', summary: '完整摘要' }],
    reportMarkdown: '# 预算方案\n\n原始正文不改变。', ...payload,
  } };
}
const feedCard = (item) => normalizeFeed({ days: [{ date: '2026-09-09', cards: [item] }] }).feed[0].cards[0];

test('maps V1 assessment separately and leaves report body and legacy quality unchanged', () => {
  const score = assessment();
  const input = row({ writingV1: true, cardAssessment: score, reportQuality: { audit_status: 'completed_unaudited' } });
  const card = feedCard(input);
  assert.equal(card.writingV1, true);
  assert.equal(card.cardAssessment.raw_total, 92.25);
  assert.equal(card.reportMarkdown, input.payload.reportMarkdown);
  assert.deepEqual(card.reportQuality, input.payload.reportQuality);
  assert.deepEqual(input.payload.cardAssessment, score);
  const legacy = feedCard(row({ reportQuality: { audit_status: 'completed', quality_score: 89 } }));
  assert.equal(legacy.writingV1, false);
  assert.equal(legacy.cardAssessment, null);
  assert.equal(legacy.reportQuality.quality_score, 89);
});

test('does not attach one payload-level assessment to multiple cards', () => {
  const input = row({ writingV1: true, cards: [{ title: '第一张', cardAssessment: assessment() }, { title: '第二张' }], cardAssessment: assessment() });
  const cards = normalizeFeed({ days: [{ date: '2026-09-09', cards: [input] }] }).feed[0].cards;
  assert.equal(cards[0].cardAssessment.raw_total, 92.25);
  assert.equal(cards[1].cardAssessment, null);
});

test('pending and missing assessments never display an invented total', () => {
  for (const score of [null, { status: 'pending', dimensions: {}, raw_total: 100, capped_reference_total: 59 }]) {
    const badge = render(WritingDraftStatus, score);
    const detail = render(CardAssessment, score);
    assert.match(badge, /V1 初稿/);
    assert.match(badge, /评分未完成/);
    assert.doesNotMatch(badge, /100|0 \/|59/);
    assert.equal((detail.match(/待评/g) ?? []).length, 5);
    assert.doesNotMatch(detail, /100|封顶参考分/);
  }
  const partial = normalizeCardAssessment(assessment({ status: 'pending', dimensions: { factual_accuracy: 12 }, missing_inputs: ['缺少原文'] }));
  assert.equal(partial.raw_total, null);
  assert.equal(partial.dimensions.factual_accuracy, 12);
  assert.match(render(CardAssessment, partial), /缺少原文/);
});

test('detail renders five approved dimensions, separate raw/capped scores and expandable evidence', () => {
  const score = assessment({ capped_reference_total: 69 });
  const html = render(CardAssessment, score);
  assert.deepEqual(ASSESSMENT_DIMENSIONS.map(({ maximum }) => maximum), [15, 15, 25, 30, 15]);
  for (const { label } of ASSESSMENT_DIMENSIONS) assert.ok(html.includes(label));
  assert.match(html, /原始总分/);
  assert.match(html, /92\.25 \/ 100/);
  assert.match(html, /封顶参考分/);
  assert.match(html, /69 \/ 100/);
  assert.match(html, /<details[^>]*><summary>查看扣分依据/);
  for (const text of ['未交代关键取舍', '方案取舍', '三万元预算', '需要比较预算限制与交付期限如何改变两个方案的优先级', '缺失内容', 'D3', '扣 3.75 分', '会议完整原文']) assert.ok(html.includes(text));
  assert.match(html, /评分不改动正文/);
  assert.match(html, /不覆盖 P2 错删或未生成的卡片/);
});

test('keeps genuine zero scores and treats malformed scored records as unfinished', () => {
  const zero = assessment({ dimensions: Object.fromEntries(ASSESSMENT_DIMENSIONS.map(({ key }) => [key, 0])), raw_total: 0 });
  assert.match(render(WritingDraftStatus, zero), /主卡评分 0 \/ 100/);
  for (const invalid of [assessment({ raw_total: '92.25' }), assessment({ dimensions: {} }), assessment({ raw_total: Number.NaN })]) {
    assert.match(render(WritingDraftStatus, invalid), /评分未完成/);
  }
});

test('assessment evidence is rendered as escaped text', () => {
  const html = render(CardAssessment, assessment({ strengths: '<script>alert(1)</script>' }));
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

test('shows a failed acceptance result even when the raw score is high', () => {
  const score = assessment({
    raw_total: 96, acceptance_total: 96, passed: false,
    dimensions: { factual_accuracy: 15, important_coverage: 15, analysis_depth: 25, actionability: 26, expression_structure: 15 },
  });
  assert.match(render(WritingDraftStatus, score), /未通过/);
  const detail = render(CardAssessment, score);
  assert.match(detail, /验收结论/);
  assert.match(detail, /未通过/);
});
