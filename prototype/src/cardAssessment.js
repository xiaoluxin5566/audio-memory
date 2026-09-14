export const ASSESSMENT_DIMENSIONS = [
  { key: 'factual_accuracy', label: '事实与证据准确性', maximum: 15 },
  { key: 'important_coverage', label: '重要内容覆盖', maximum: 15 },
  { key: 'analysis_depth', label: '分析深度', maximum: 25 },
  { key: 'actionability', label: '建议与行动质量', maximum: 30 },
  { key: 'expression_structure', label: '表达与结构', maximum: 15 },
];

const boundedScore = (value, maximum) => typeof value === 'number'
  && Number.isFinite(value) && value >= 0 && value <= maximum ? value : null;

export function normalizeCardAssessment(value) {
  if (!value || typeof value !== 'object' || !['pending', 'scored'].includes(value.status)) return null;
  const dimensions = Object.fromEntries(ASSESSMENT_DIMENSIONS.map(({ key, maximum }) =>
    [key, boundedScore(value.dimensions?.[key], maximum)]));
  const rawTotal = boundedScore(value.raw_total, 100);
  const scored = value.status === 'scored' && rawTotal !== null
    && Object.values(dimensions).every((score) => score !== null);
  return {
    ...value,
    status: scored ? 'scored' : 'pending',
    dimensions,
    raw_total: scored ? rawTotal : null,
    capped_reference_total: scored ? boundedScore(value.capped_reference_total, rawTotal) : null,
    acceptance_total: scored ? boundedScore(value.acceptance_total, 100) : null,
    passed: scored && typeof value.passed === 'boolean' ? value.passed : null,
    deductions: Array.isArray(value.deductions) ? value.deductions.filter((item) => item && typeof item === 'object') : [],
    read_scope: Array.isArray(value.read_scope) ? value.read_scope : [],
    missing_inputs: Array.isArray(value.missing_inputs) ? value.missing_inputs : [],
  };
}

export function assessmentStatusLabel(assessment) {
  if (assessment?.status !== 'scored' || boundedScore(assessment.raw_total, 100) === null) {
    return '评分未完成';
  }
  const outcome = assessment.passed === true ? ' · 通过'
    : assessment.passed === false ? ' · 未通过' : '';
  return `主卡评分 ${assessment.raw_total} / 100${outcome}`;
}

export function assessmentText(value) {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(assessmentText).filter(Boolean).join('\n');
  return JSON.stringify(value, null, 2);
}
