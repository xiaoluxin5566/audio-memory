import React from 'react';
import { ASSESSMENT_DIMENSIONS, assessmentStatusLabel, assessmentText, normalizeCardAssessment } from '../cardAssessment.js';
import './CardAssessment.css';

export function WritingDraftStatus({ assessment }) {
  const record = normalizeCardAssessment(assessment);
  return <div className="writing-draft-status"><span>V1 初稿</span><span>{assessmentStatusLabel(record)}</span></div>;
}

export function CardAssessment({ assessment }) {
  const record = normalizeCardAssessment(assessment);
  const scored = record?.status === 'scored';
  const deductions = record?.deductions ?? [];
  return <section className="card-assessment" aria-label="主卡评分">
    <div className="card-assessment-heading"><h2>主卡评分</h2><span>{scored ? record.passed === true ? '验收通过' : record.passed === false ? '验收未通过' : '评分已完成' : '评分未完成'}</span></div>
    <p className="card-assessment-note">独立评价这张已生成的 V1 主卡，评分不改动正文；不覆盖 P2 错删或未生成的卡片，不代表全天报告覆盖度或正式审核通过。</p>
    <table className="card-assessment-table"><thead><tr><th scope="col">评分维度</th><th scope="col">得分 / 满分</th></tr></thead>
      <tbody>{ASSESSMENT_DIMENSIONS.map(({ key, label, maximum }) => <tr key={key}><th scope="row">{label}</th><td>{record?.dimensions[key] ?? '待评'} <span>/ {maximum}</span></td></tr>)}</tbody>
      <tfoot><tr><th scope="row">原始总分</th><td>{scored ? `${record.raw_total} / 100` : '评分未完成'}</td></tr>
        {scored && record.capped_reference_total !== null && <tr><th scope="row">封顶参考分</th><td>{record.capped_reference_total} / 100</td></tr>}
        {scored && record.passed !== null && <tr><th scope="row">验收结论</th><td>{record.passed ? '通过' : '未通过'}</td></tr>}</tfoot>
    </table>
    {record?.strengths && <div className="card-assessment-observation"><h3>已交付的价值</h3><p>{assessmentText(record.strengths)}</p></div>}
    {record?.weaknesses && <div className="card-assessment-observation"><h3>主要不足</h3><p>{assessmentText(record.weaknesses)}</p></div>}
    {Boolean(record?.missing_inputs.length) && <div className="card-assessment-observation"><h3>尚缺评价材料</h3><p>{assessmentText(record.missing_inputs)}</p></div>}
    {deductions.length > 0 ? <details className="card-assessment-deductions"><summary>查看扣分依据 · {deductions.length} 项</summary>
      <ol>{deductions.map((item, index) => <li key={`${item.rule ?? 'deduction'}-${index}`}>
        <div className="card-assessment-deduction-heading"><strong>{ASSESSMENT_DIMENSIONS.find(({ key }) => key === item.dimension)?.label ?? '扣分项'}</strong><span>{item.rule}{typeof item.points === 'number' && Number.isFinite(item.points) ? ` · 扣 ${item.points} 分` : ''}</span></div>
        <p>{assessmentText(item.reason)}</p><dl><dt>正文位置</dt><dd>{assessmentText(item.body_locator) || '未提供'}</dd><dt>扣分依据</dt><dd>{assessmentText(item.evidence) || '未提供'}</dd><dt>缺失内容</dt><dd>{assessmentText(item.missing_content) || '未提供'}</dd></dl>
      </li>)}</ol>
    </details> : scored && <p className="card-assessment-note">本次评价未记录扣分项。</p>}
    {Boolean(record?.read_scope.length) && <details className="card-assessment-scope"><summary>已读取的评价材料</summary><p>{assessmentText(record.read_scope)}</p></details>}
  </section>;
}
