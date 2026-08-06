export function ReanalysisModal({ preview, loading, error, current, view, onClose, onConfirm, onAction }) {
  const counts = view?.counts
  return <div className="modal-backdrop"><section className="modal reanalysis-modal" role="dialog" aria-modal="true" aria-labelledby="reanalysis-modal-title">
    <button className="modal-close" onClick={onClose} aria-label="关闭重新分析">×</button>
    <h1 id="reanalysis-modal-title">重新分析历史</h1>
    {loading && <p>正在读取本次重新分析范围…</p>}
    {error && <div className="inline-error"><b>{error}</b></div>}
    {preview && <>
      <p>将使用当前模型和最新 Prompt 重新生成历史结果；原始音频与转写不会变化。</p>
      <div className="reanalysis-facts"><b>{preview.batchCount} 个上传批次 · {preview.fileCount} 个音频文件</b><span>{preview.characterCount.toLocaleString('zh-CN')} 个字符</span><span>{preview.modelLabel}</span><span>预计 {preview.callRange}</span></div>
      <div className="reanalysis-prompts"><b>Prompt 版本</b><div>{preview.promptVersions.map((prompt) => <span key={prompt.sceneId}>{prompt.label}</span>)}</div></div>
      <div className="reanalysis-no-whisper">不会重新转写，也不会重新进行说话人识别。</div>
      <div className="reanalysis-cost">{preview.costNotice}</div>
      {preview.blockers.length > 0 && <div className="inline-error"><b>{preview.blockers.join('；')}</b></div>}
    </>}
    {current && counts && <div className="reanalysis-progress"><b>{view.completionCopy || '重新分析进度'}</b><div className="reanalysis-counts"><span>成功 {counts.succeeded}</span><span>失败 {counts.failed}</span><span>待处理 {counts.pending}</span><span>已停止 {counts.stopped}</span></div></div>}
    <div className="modal-actions">
      <button className="secondary" onClick={onClose}>关闭</button>
      {view?.state === 'running' && <button className="danger-solid" onClick={onAction}>停止重新分析</button>}
      {view?.state === 'paused' && <button className="primary" onClick={onAction}>继续重新分析</button>}
      {view?.actionLabel === '重试画像更新' && <button className="primary" onClick={onAction}>重试画像更新</button>}
      {!current && <button className="primary" disabled={!preview?.previewToken || preview.blockers.length > 0} onClick={onConfirm}>确认重新分析</button>}
    </div>
  </section></div>
}
