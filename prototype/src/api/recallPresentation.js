export function readinessMessage(status) {
  if (!status) return '正在连接问答服务…';
  if (status.can_query === false && status.availability_message) return status.availability_message;
  if (!status.configured) return ({
    configuration_not_ready: '问答模型或向量服务尚未配置，暂时无法发送问题。',
    cloud_authorization_required: '当前服务缺少已确认的模型调用授权，暂时无法发送问题。',
    source_revision_not_authorized: '当前资料版本不在授权范围内，暂时无法发送问题。',
  })[status.error_code] || `问答服务尚未就绪（${status.error_code || '原因未记录'}）。`;
  return retrievableCount(status) === 0 ? '当前没有已就绪的事件或其他片段，请先上传并完成处理。' : '';
}

export function durationLabel(milliseconds) {
  return typeof milliseconds === 'number' && Number.isFinite(milliseconds) ? `${(milliseconds / 1000).toFixed(2)} 秒` : '未记录';
}

export async function loadDatabaseViews(api, setTables, setUploads, setError) {
  await Promise.allSettled([
    api.databaseTables().then(data => setTables(data.tables)).catch(error => setError(`数据库目录读取失败：${error.message}`)),
    api.uploads().then(data => setUploads(data.uploads)).catch(error => setError(`上传记录读取失败：${error.message}`)),
  ]);
}


export function eventCountLabel(status) {
  if (!status) return '正在读取数据范围';
  if (Number.isInteger(status.ready_recording_event_count) && Number.isInteger(status.ready_reported_event_count)) {
    const events = `${status.ready_recording_event_count} 个现场事件 · ${status.ready_reported_event_count} 个被提及事件`;
    return Number.isInteger(status.ready_other_fragment_count) ? `${events} · ${status.ready_other_fragment_count} 段其他片段` : events;
  }
  return `${status.ready_event_count} 条事件记录（类型数量未记录）`;
}

export function retrievableCount(status) {
  return status?.ready_retrievable_count ?? status?.ready_event_count ?? 0;
}

export function sourceCountLabel(records) {
  const others = records.filter(record => record.recording_kind === 'other_fragment').length;
  const events = records.length - others;
  return others ? `${events} 个事件 / ${others} 段其他片段` : `${events} 个事件`;
}
