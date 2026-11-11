export const DATABASE_PAGE_SIZE = 100;

export function nullValueLabel(columnName) {
  return ['owner_entity_id', 'local_owner'].includes(columnName) ? '负责人未知' : 'NULL';
}

export function fieldTooltipPosition(anchor, viewport, size) {
  return {
    left: Math.max(12, Math.min(anchor.left, viewport.width - size.width - 12)),
    top: anchor.bottom + 8 + size.height <= viewport.height - 12
      ? anchor.bottom + 8 : Math.max(12, anchor.top - size.height - 8),
  };
}

export function completeFactRows(rows) {
  return rows.map(row => ({
    id: row.id,
    '主体': row.subject, '属性': row.property, '值': row.value, '单位': row.unit,
    '版本生效日期': row.version_start_date, '事项截止日期': row.deadline_date, '版本结束日期': row.version_end_date,
    '所属父事件': eventTitles(row, 'parent_event_title'),
    '所属子事件': eventTitles(row, 'child_event_title'),
    '事件与原文': row,
  }));
}

function eventTitles(row, field) {
  const titles = row.occurrences.map(item => item[field]).filter(Boolean);
  return [...new Set(titles)].join('、') || null;
}
