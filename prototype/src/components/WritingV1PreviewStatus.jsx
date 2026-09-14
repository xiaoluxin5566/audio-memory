import React, { useEffect, useState } from 'react';

function isStoppedWithoutNewCards(value) {
  return value?.status === 'stopped' && value.new_card_count === 0
    && value.scored_card_count === 0 && typeof value.failure_summary === 'string';
}

export async function loadWritingV1PreviewStatus({ enabled = false, fetcher = globalThis.fetch } = {}) {
  if (!enabled) return null;
  try {
    const response = await fetcher('/writing-v1-status.json', { method: 'GET', cache: 'no-store', credentials: 'same-origin' });
    if (!response.ok) return null;
    const value = await response.json();
    return isStoppedWithoutNewCards(value) ? value : null;
  } catch {
    return null;
  }
}

export function WritingV1StoppedNotice({ status }) {
  if (!isStoppedWithoutNewCards(status)) return null;
  return <section className="job-card warning" role="status" aria-live="polite">
    <b>本轮 V1 尚未生成</b>
    <p>生成中途停止，暂无新报告和评分。下方显示历史报告。</p>
    <p>{status.failure_summary}</p>
  </section>;
}

export function WritingV1PreviewStatus({ enabled = false }) {
  const [status, setStatus] = useState(null);
  useEffect(() => {
    let cancelled = false;
    loadWritingV1PreviewStatus({ enabled }).then((value) => {
      if (!cancelled) setStatus(value);
    });
    return () => { cancelled = true; };
  }, [enabled]);
  return enabled ? <WritingV1StoppedNotice status={status} /> : null;
}
