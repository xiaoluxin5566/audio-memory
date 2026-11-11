import { useEffect, useState } from 'react';
import { recallApi } from '../api/recall.js';
import { durationLabel, sourceCountLabel } from '../api/recallPresentation.js';
import { DatabaseGrid } from './DatabaseGrid.jsx';

export function ReplyBrief({ turnId }) {
  const [brief, setBrief] = useState(null), [error, setError] = useState(''), [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    recallApi.answerBrief(turnId).then(data => { if (live) { setBrief(data); setError(''); } }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [turnId, revision]);
  if (error) return <p className="db-caption">简述读取失败：{error} <button className="secondary compact" onClick={() => setRevision(v => v + 1)}>重试简述</button></p>;
  if (!brief) return <p className="db-caption" role="status">正在生成本次回答简述…</p>;
  const rows = brief.linked_facts.map(f => ({ '事实编号': f.fact_id, '陈述编号': f.occurrence_id, '主体': `${f.subject_name} · ${f.subject_id}`, '属性': `${f.property_name} · ${f.property_id}`, '值': f.value, '单位': f.unit, '证据': f.evidence }));
  return <section className="reply-brief" aria-label="本次回答简述"><p className="db-caption">模型请求：{brief.model_calls ?? '未记录'} 次（生成 {brief.generation_calls ?? '未记录'} / 向量 {brief.embedding_calls ?? '未记录'}，包含失败尝试） · 回答耗时：{durationLabel(brief.answer_ms)} · 简述生成耗时：{durationLabel(brief.brief_ms)}</p><details><summary>引用与结构化记录 · {sourceCountLabel(brief.events)} / {rows.length} 条关联事实</summary><p className="db-caption">{brief.linkage_note} 简述由日志生成，额外模型调用 {brief.brief_model_calls} 次。回答耗时为处理时间，不含排队；历史未记录的耗时不作估算。{brief.unavailable_citations > 0 && ` 另有 ${brief.unavailable_citations} 处引用的来源已不可用。`}</p>{brief.events.map(e => <p key={e.id}><b>{e.title}</b> · <code>{e.event_id}</code> · 版本 <code>{e.id}</code></p>)}<DatabaseGrid columns={['事实编号', '陈述编号', '主体', '属性', '值', '单位', '证据'].map(name => ({ name }))} rows={rows} empty="本次引用没有可明确关联的结构化事实；请查看回答的原文引用。" /></details></section>;
}
