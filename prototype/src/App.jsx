import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createInitialState,
  formatJobEta,
  getFeedbackFormState,
  jobFailureCopy,
  jobProgressValue,
  orderCards,
} from './store.js';
import { api } from './api/client.js';
import { uploadFile } from './api/upload.js';
import { analysisBlocks, normalizeFeed, normalizeHistory } from './api/state.js';
import { useProviders } from './hooks/useProviders.js';
import { useActiveJob } from './hooks/useActiveJob.js';
import { useReanalysis } from './hooks/useReanalysis.js';
import { ReanalysisModal } from './components/ReanalysisModal.jsx';
import { getReanalysisView, isActiveReanalysis } from './api/state.js';
import { buildReportEventMap } from './reportPresentation.js';
import './styles.css';

const ROUTES = { '/': 'feed', '/history': 'history' };
const ROUTE_PATHS = { feed: '/', history: '/history' };
const sceneClass = { analysis: 'analysis', meeting: 'meeting', parenting: 'parenting', content: 'content', growth: 'growth', inspiration: 'inspiration' };
const providerStateLabel = {
  initializing: '正在读取本地配置',
  unconfigured: '等待填写 API Key',
  validating: '正在校验',
  available: '连接可用',
  unavailable: '连接不可用',
  keychain_unavailable: '无法访问系统钥匙串',
};

function prettySize(bytes = 0) {
  if (!bytes) return '本地文件';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function App() {
  const [state, setState] = useState(() => createInitialState());
  const providerState = useProviders();
  const [route, setRoute] = useState(ROUTES[window.location.pathname] ?? 'feed');
  const [providerOpen, setProviderOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [reanalysisOpen, setReanalysisOpen] = useState(false);
  const [sleepPromptOpen, setSleepPromptOpen] = useState(false);
  const [analysisSettings, setAnalysisSettings] = useState({ preventSleep: false, status: 'inactive', loaded: false });
  const [selectedCard, setSelectedCard] = useState(null);
  const [toast, setToast] = useState('');
  const [editingTodo, setEditingTodo] = useState(null);
  const fileInput = useRef(null);
  const pendingUploadFiles = useRef([]);
  const lastFinishedReanalysis = useRef(null);
  const reportPreviewOpened = useRef(false);
  const reanalysis = useReanalysis();

  const refreshContent = useCallback(async () => {
    const [feedPayload, historyPayload] = await Promise.all([api.feed(), api.history()]);
    const normalized = normalizeFeed(feedPayload);
    setState((current) => ({
      ...current,
      feed: normalized.feed,
      todos: normalized.todos,
      history: normalizeHistory(historyPayload),
    }));
  }, []);

  useEffect(() => {
    setState((current) => ({
      ...current,
      providers: providerState.providers,
      activeProvider: providerState.activeProvider,
    }));
  }, [providerState.providers, providerState.activeProvider]);

  useEffect(() => {
    refreshContent().catch(() => setToast('无法读取本地历史，请确认服务已启动'));
  }, [refreshContent]);
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('reportPreview') !== 'deepseek') return;
    if (reportPreviewOpened.current) return;
    const batch = state.feed[0];
    const card = batch?.cards.find((item) => item.reportMarkdown);
    if (batch && card) {
      reportPreviewOpened.current = true;
      setSelectedCard({ card, batch });
    }
  }, [state.feed]);
  useEffect(() => {
    api.analysisSettings().then((settings) => setAnalysisSettings({
      preventSleep: Boolean(settings.prevent_sleep),
      status: settings.sleep_prevention_status || 'inactive',
      loaded: true,
    })).catch(() => setAnalysisSettings((current) => ({ ...current, loaded: true })));
  }, []);
  useEffect(() => {
    api.activeJob().then((job) => {
      if (!job) return;
      setState((current) => ({
        ...current,
        job: { ...job, progress: jobProgressValue(job) },
        upload: {
          files: (job.files ?? []).map((file) => ({
            id: file.id,
            name: file.original_name,
            size: file.size_bytes,
            type: file.extension?.slice(1).toUpperCase() || 'AUDIO',
            progress: file.upload_progress ?? 100,
            invalid: false,
          })),
          error: '',
          paused: false,
        },
      }));
    }).catch(() => {});
  }, []);
  useEffect(() => {
    const onPop = () => setRoute(ROUTES[window.location.pathname] ?? 'feed');
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(''), 2500);
    return () => clearTimeout(timer);
  }, [toast]);
  const reanalysisView = getReanalysisView(reanalysis.current, reanalysis.preview ?? { source_batch_count: state.history.length });
  useEffect(() => {
    const batch = reanalysis.current;
    if (!batch || !['completed', 'completed_with_failures', 'content_completed_profile_failed', 'stopped'].includes(batch.status) || lastFinishedReanalysis.current === `${batch.id}:${batch.status}`) return;
    lastFinishedReanalysis.current = `${batch.id}:${batch.status}`;
    refreshContent().catch(() => {});
  }, [reanalysis.current?.id, reanalysis.current?.status, refreshContent]);

  async function openReanalysis() {
    setReanalysisOpen(true);
    if (!isActiveReanalysis(reanalysis.current)) await reanalysis.loadPreview().catch(() => {});
  }
  async function confirmReanalysis() {
    if (!reanalysis.preview?.previewToken) return;
    try { await reanalysis.start(reanalysis.preview.previewToken); setToast('已开始使用最新 Prompt 重新分析历史'); }
    catch (error) { setToast(error.message); }
  }
  async function controlReanalysis() {
    try {
      if (reanalysisView.state === 'running') await reanalysis.stop();
      else if (reanalysisView.state === 'paused') await reanalysis.resume();
      else if (reanalysis.current?.status === 'stopped') await reanalysis.resume();
      else if (reanalysisView.actionLabel === '重试画像更新') await reanalysis.retryProfile();
    } catch (error) { setToast(error.message); }
  }
  function closeReanalysis() {
    reanalysis.dismissPreview();
    setReanalysisOpen(false);
  }

  function navigate(nextRoute) {
    setSelectedCard(null);
    setRoute(nextRoute);
    window.history.pushState({}, '', ROUTE_PATHS[nextRoute]);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function addFiles(fileList) {
    if (state.providers[state.activeProvider]?.state !== 'available') {
      setProviderOpen(true);
      return;
    }
    const files = [...fileList];
    if (!files.length) return;
    let jobId = state.job?.id;
    if (!jobId) jobId = (await api.createJob()).id;
    setState((current) => ({ ...current, job: { id: jobId, stage: 'uploading', progress: 0 } }));
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      const localId = crypto.randomUUID();
      const pending = { id: localId, name: file.name, size: file.size, type: file.name.split('.').pop()?.toUpperCase(), progress: 0, invalid: false };
      setState((current) => ({ ...current, upload: { ...current.upload, files: [...current.upload.files, pending], error: '' } }));
      try {
        const uploaded = await uploadFile(jobId, file, { onProgress: (progress) => {
          setState((current) => ({ ...current, upload: { ...current.upload, files: current.upload.files.map((item) => item.id === localId ? { ...item, progress } : item) } }));
        } });
        setState((current) => ({ ...current, upload: { ...current.upload, files: current.upload.files.map((item) => item.id === localId ? { ...item, id: uploaded.id, progress: 100, type: uploaded.extension.slice(1).toUpperCase() } : item) } }));
      } catch (error) {
        if (error.code === 'unsupported_format') pendingUploadFiles.current = files.slice(index + 1);
        setState((current) => ({ ...current, upload: { files: current.upload.files.map((item) => item.id === localId ? { ...item, id: error.fileId || localId, invalid: true } : item), error: error.message, paused: error.code === 'unsupported_format' } }));
        break;
      }
    }
  }

  async function removeFile(id) {
    const removedInvalidFile = state.upload.files.some((file) => file.id === id && file.invalid);
    if (state.job?.id) await api.removeFile(state.job.id, id);
    setState((current) => {
      const files = current.upload.files.filter((file) => file.id !== id);
      return { ...current, upload: { files, error: files.some((file) => file.invalid) ? current.upload.error : '', paused: files.some((file) => file.invalid) } };
    });
    if (removedInvalidFile && pendingUploadFiles.current.length) {
      const pending = pendingUploadFiles.current;
      pendingUploadFiles.current = [];
      await addFiles(pending);
    }
  }

  async function executeStartAnalysis() {
    const provider = state.providers[state.activeProvider];
    if (provider?.state !== 'available') { setProviderOpen(true); return; }
    if (!state.upload.files.length || state.upload.paused || state.upload.files.some((file) => file.progress < 100)) return;
    const job = await api.startJob(state.job.id);
    const sleepStatus = job.sleep_prevention_status || 'disabled';
    setAnalysisSettings((current) => ({ ...current, status: sleepStatus }));
    if (sleepStatus === 'unavailable') setToast('防休眠未生效，请保持电脑唤醒以完成分析');
    setState((current) => ({ ...current, job: { ...job, progress: jobProgressValue(job) } }));
  }

  async function startAnalysis() {
    const provider = state.providers[state.activeProvider];
    if (provider?.state !== 'available') { setProviderOpen(true); return; }
    if (!state.upload.files.length || state.upload.paused || state.upload.files.some((file) => file.progress < 100)) return;
    if (!analysisSettings.loaded) return;
    if (!analysisSettings.preventSleep) { setSleepPromptOpen(true); return; }
    await executeStartAnalysis();
  }

  async function updatePreventSleep(enabled) {
    try {
      const settings = await api.updateAnalysisSettings(enabled);
      setAnalysisSettings({ preventSleep: Boolean(settings.prevent_sleep), status: settings.sleep_prevention_status || 'inactive', loaded: true });
      return true;
    } catch (error) {
      setToast(error.message);
      return false;
    }
  }

  async function enableSleepPreventionAndStart() {
    if (!await updatePreventSleep(true)) return;
    setSleepPromptOpen(false);
    await executeStartAnalysis();
  }

  const onJobUpdate = useCallback((job) => {
    const progress = jobProgressValue(job);
    setState((current) => ({ ...current, job: { ...current.job, ...job, progress } }));
    if (job.sleep_prevention_status) {
      setAnalysisSettings((current) => ({ ...current, status: job.sleep_prevention_status }));
    }
    if (['failed', 'interrupted', 'completed'].includes(job.stage)) {
      setAnalysisSettings((current) => ({ ...current, status: 'inactive' }));
    }
  }, []);
  const onJobComplete = useCallback(async () => {
    await refreshContent();
    setState((current) => ({ ...current, upload: { files: [], error: '', paused: false }, job: null }));
    setToast('分析完成，新结果已添加到信息流');
    setAnalysisSettings((current) => ({ ...current, status: 'inactive' }));
  }, [refreshContent]);
  const watchedJobId = ['transcribing', 'analyzing'].includes(state.job?.stage)
    ? state.job.id
    : null;
  useActiveJob(watchedJobId, onJobUpdate, onJobComplete);

  async function resumeJob() {
    if (state.job.stage === 'failed') {
      const result = await api.retryAnalysis(state.job.id);
      setAnalysisSettings((current) => ({ ...current, status: result.sleep_prevention_status || current.status }));
      if (result.sleep_prevention_status === 'unavailable') setToast('防休眠未生效，请保持电脑唤醒以完成分析');
      setState((current) => ({ ...current, job: { ...current.job, stage: 'analyzing', progress: 70, error_code: null } }));
      return;
    }
    const result = await api.resumeJob(state.job.id);
    setAnalysisSettings((current) => ({ ...current, status: result.sleep_prevention_status || current.status }));
    if (result.sleep_prevention_status === 'unavailable') setToast('防休眠未生效，请保持电脑唤醒以完成分析');
    setState((current) => ({ ...current, job: { ...current.job, stage: 'transcribing', progress: 0 } }));
  }
  async function cancelJob() {
    if (state.job?.id) await api.cancelJob(state.job.id);
    setState((current) => ({ ...current, job: null, upload: { files: [], error: '', paused: false } }));
    setAnalysisSettings((current) => ({ ...current, status: 'inactive' }));
  }

  const currentProvider = state.providers[state.activeProvider];
  return (
    <div className="app-shell">
      <Topbar route={route} onNavigate={navigate} reanalysis={reanalysisView} onReanalyze={openReanalysis} onClear={() => setClearOpen(true)} />
      {route === 'feed' && (
        <div className="home-layout">
          <aside className="control-rail">
            <h1>分析音频</h1>
            <section className="panel provider-panel">
              <div className="panel-title"><strong>模型与 API Key</strong><span className={currentProvider.configured ? 'ok-text' : ''}>{currentProvider.configured ? '已配置' : '未配置'}</span></div>
              <div className="provider-summary"><div>{currentProvider.configured && <b>{currentProvider.modelName}</b>}<small>{currentProvider.configured ? '用于内容分析' : '请先完成配置'}</small></div><button className="secondary compact" onClick={() => setProviderOpen(true)}>{currentProvider.configured ? '修改' : '去配置'}</button></div>
              {currentProvider.state === 'available' && <div className="status-success"><i />连接可用 · {currentProvider.lastChecked || '刚刚'} 校验</div>}
              {currentProvider.error && <div className="inline-error"><b>{currentProvider.error}</b></div>}
            </section>
            <section className="panel sleep-setting-panel">
              <label className="sleep-setting-row">
                <span><strong>分析期间保持电脑唤醒</strong><small>锁屏或长时间不操作时，转写和报告生成仍可继续</small></span>
                <input type="checkbox" role="switch" aria-label="分析期间保持电脑唤醒" checked={analysisSettings.preventSleep} disabled={!analysisSettings.loaded || analysisSettings.status === 'active'} onChange={(event) => updatePreventSleep(event.target.checked)} />
              </label>
              {analysisSettings.status === 'active' && <div className="sleep-protection-active"><i />保护中 · 屏幕仍可正常关闭</div>}
              {analysisSettings.status === 'unavailable' && <div className="sleep-protection-error">防休眠未生效，请保持电脑唤醒</div>}
            </section>
            <section className="panel upload-panel">
              <div className="panel-title"><strong>上传音频</strong><span>{state.upload.files.length ? `${state.upload.files.length} 个文件` : ''}</span></div>
              <div className={`drop-zone ${currentProvider.state !== 'available' ? 'disabled' : ''}`} onClick={() => currentProvider.state === 'available' ? fileInput.current?.click() : setProviderOpen(true)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); addFiles(event.dataTransfer.files); }}>
                <b>拖拽音频到这里，或点击选择</b><span>支持 MP3、AAC</span>
                <input ref={fileInput} type="file" multiple disabled={currentProvider.state !== 'available'} accept=".mp3,.aac,audio/mpeg,audio/aac" onChange={(event) => { addFiles(event.target.files); event.target.value = ''; }} />
              </div>
              {state.upload.error && <div className="inline-error"><b>{state.upload.error}</b><span>移除不支持的文件后可继续。</span></div>}
              <div className="file-stack">{state.upload.files.map((file) => <UploadFile key={file.id} file={file} onRemove={() => removeFile(file.id)} />)}</div>
              {state.job && state.job.stage !== 'uploading' ? <JobPanel job={state.job} onRetry={resumeJob} onCancel={cancelJob} /> : (
                <button className="primary full" disabled={!analysisSettings.loaded || !state.upload.files.length || state.upload.paused || state.upload.files.some((file) => file.progress < 100)} onClick={startAnalysis}>开始分析{state.upload.files.length ? ` ${state.upload.files.length} 个文件` : ''}</button>
              )}
              <p className="privacy">音频、转写和结果保存在本机；只有转写文本会发送给当前模型厂商。</p>
            </section>
          </aside>
          <main className="feed-area">
            {selectedCard ? <CardDetail card={selectedCard.card} batch={selectedCard.batch} onClose={() => setSelectedCard(null)} onToast={setToast} /> : <Feed state={state} refresh={refreshContent} editingTodo={editingTodo} setEditingTodo={setEditingTodo} onOpenCard={(card, batch) => setSelectedCard({ card, batch })} />}
          </main>
        </div>
      )}
      {route === 'history' && <History state={state} />}
      {providerOpen && <ProviderModal state={state} refresh={providerState.refresh} onClose={() => setProviderOpen(false)} onToast={setToast} />}
      {reanalysisOpen && <ReanalysisModal preview={reanalysis.preview} loading={reanalysis.loadingPreview} error={reanalysis.error} current={reanalysis.current} view={reanalysisView} onClose={closeReanalysis} onConfirm={confirmReanalysis} onAction={controlReanalysis} />}
      {sleepPromptOpen && <SleepPreventionPrompt onEnable={enableSleepPreventionAndStart} onContinue={async () => { setSleepPromptOpen(false); await executeStartAnalysis(); }} onClose={() => setSleepPromptOpen(false)} />}
      {clearOpen && <ClearModal onClose={() => setClearOpen(false)} onConfirm={async () => { await api.clearHistory(); reanalysis.clearState(); setState((current) => ({ ...current, feed: [], todos: [], history: [] })); await refreshContent(); setSelectedCard(null); setClearOpen(false); setToast('所有历史已清除'); navigate('feed'); }} />}
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}

function SleepPreventionPrompt({ onEnable, onContinue, onClose }) {
  return <div className="modal-backdrop"><section className="modal sleep-prompt-modal" role="dialog" aria-modal="true" aria-labelledby="sleep-prompt-title"><button className="modal-close" onClick={onClose} aria-label="关闭">×</button><div className="sleep-mark">☾</div><h1 id="sleep-prompt-title">分析期间保持电脑唤醒？</h1><p>电脑进入休眠后，转写和报告生成会暂停。开启后，即使锁屏或长时间不操作，分析仍会继续；屏幕仍可正常关闭。</p><div className="modal-actions"><button className="secondary" onClick={onContinue}>暂不开启</button><button className="primary" onClick={onEnable}>开启并继续</button></div></section></div>;
}

function Topbar({ route, onNavigate, reanalysis, onReanalyze, onClear }) {
  return <header className="topbar"><div className="brand"><div className="brand-mark">AM</div><div><b>Audio Memory</b><span>本地音频智能分析</span></div></div><div className="top-actions"><nav>{[['feed', '信息流'], ['history', '音频历史']].map(([id, label]) => <button key={id} className={route === id ? 'active' : ''} onClick={() => onNavigate(id)}>{label}</button>)}</nav><button className="secondary reanalysis-entry" disabled={reanalysis.state === 'disabled' || reanalysis.state === 'stopping'} onClick={onReanalyze}>{reanalysis.buttonLabel}</button><button className="danger-ghost" disabled={!reanalysis.canClearHistory} onClick={onClear}>清除所有历史</button></div></header>;
}

function UploadFile({ file, onRemove }) {
  return <div className={`upload-file ${file.invalid ? 'invalid' : ''}`}><div className="file-type">{file.type}</div><div className="file-main"><b>{file.name}</b><span>{prettySize(file.size)} · {file.invalid ? '不支持的格式' : file.progress === 100 ? '上传完成' : `上传中 ${file.progress}%`}</span>{!file.invalid && file.progress < 100 && <div className="progress"><i style={{ width: `${file.progress}%` }} /></div>}</div><button className="icon-button" onClick={onRemove} aria-label={`移除 ${file.name}`}>×</button></div>;
}

function JobPanel({ job, onRetry, onCancel }) {
  if (job.stage === 'interrupted') return <div className="job-card warning"><b>发现未完成的分析任务</b><p>上次处理在中断前已保存进度，可以从中断位置继续。</p><div><button className="secondary" onClick={onCancel}>取消任务</button><button className="primary" onClick={onRetry}>继续分析</button></div></div>;
  if (job.stage === 'failed') {
    const failure = jobFailureCopy(job);
    return <div className="job-card error"><b>{failure.title}</b>{job.error_code && <code>{job.error_code}</code>}<p>{failure.body}</p><div><button className="secondary" onClick={onCancel}>放弃任务</button><button className="primary" onClick={onRetry}>{failure.action}</button></div></div>;
  }
  const transcribing = job.stage === 'transcribing';
  const phase = transcribing ? `${job.local_phase || '准备本地转写'}${job.batch_total ? ` ${job.batch_current}/${job.batch_total}` : ''}` : 'DeepSeek 正在阅读全文并生成报告';
  return <div className="job-card"><div className="job-title"><b>{phase}</b><span>{Math.round(job.progress * 10) / 10}%</span></div><div className="progress large"><i style={{ width: `${job.progress}%` }} /></div><p className="job-eta">{formatJobEta(job)}</p>{transcribing && <p>快速转写（Beta）可能遗漏低音量、远场或重叠语音，也可能把背景媒体识别为对话。关键人物、数字、日期和待办请回听原音频确认。</p>}<div className="stage-row done"><i />音频上传<span>已完成</span></div><div className={`stage-row ${transcribing ? 'doing' : 'done'}`}><i />本地转写与时间轴校验<span>{transcribing ? '进行中' : '已完成'}</span></div><div className={`stage-row ${transcribing ? 'waiting' : 'doing'}`}><i />生成全天报告<span>{transcribing ? '等待中' : '进行中'}</span></div>{transcribing ? <button className="secondary full" onClick={onCancel}>取消本次分析</button> : <p>报告正在安全发布，完成前请保持应用运行。</p>}</div>;
}

function Feed({ state, refresh, editingTodo, setEditingTodo, onOpenCard }) {
  if (!state.feed.length && !state.todos.length) return <div className="empty-feed"><div className="empty-mark">AM</div><h2>先上传音频</h2><p>分析完成后，系统会自主判断值得关注的内容，并生成深度分析卡片。</p></div>;
  const incomplete = state.todos.filter((todo) => !todo.completed);
  const completed = state.todos.filter((todo) => todo.completed);
  return <div className="feed-content">{state.todos.length > 0 && <section className="todo-card"><div className="todo-head"><h2>全局待办</h2><span>{incomplete.length} 项未完成</span></div>{incomplete.map((todo) => <TodoRow key={todo.id} todo={todo} refresh={refresh} editingTodo={editingTodo} setEditingTodo={setEditingTodo} />)}{completed.length > 0 && <><div className="completed-label">已完成 · {completed.length}</div>{completed.map((todo) => <TodoRow key={todo.id} todo={todo} refresh={refresh} editingTodo={editingTodo} setEditingTodo={setEditingTodo} />)}</>}</section>}{state.feed.map((batch) => {
    const overview = batch.cards.find((card) => card.kind === 'batch_overview');
    const cards = orderCards(batch.cards.filter((card) => card.kind !== 'batch_overview'));
    return <section className="day-block" key={batch.id}><div className="date-divider"><b>{batch.date}</b><span>最新更新 {batch.uploadedAt}</span></div><div className="batch-line"><div className="batch-title"><b>{batch.uploadedAt} 上传</b><span>分析完成</span></div>{overview && <BatchOverview overview={overview} />}{cards.map((card) => <article className="result-card" key={card.id} onClick={() => onOpenCard(card, batch)}><div className="result-head"><span className={`scene-badge ${sceneClass[card.sceneId]}`}>{card.label}</span><small>{card.timeLabel}</small></div><h3>{card.title}</h3><p>{card.summary}</p><div className="result-foot"><span>{card.meta}</span><button>查看完整结果 ›</button></div></article>)}</div></section>;
  })}</div>;
}

function BatchOverview({ overview }) {
  return <section className="batch-overview"><div className="batch-overview-kicker">BATCH OVERVIEW</div><h2>{overview.title}</h2>{overview.summary && <p>{overview.summary}</p>}</section>;
}

function toDateTimeLocal(value) {
  if (!value || Number.isNaN(new Date(value).getTime())) return '';
  const date = new Date(value);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function TodoRow({ todo, refresh, editingTodo, setEditingTodo }) {
  const [draft, setDraft] = useState(todo.text);
  const [dueAtDraft, setDueAtDraft] = useState(() => toDateTimeLocal(todo.dueAt));
  const editing = editingTodo === todo.id;
  return <div className={`todo-row ${todo.completed ? 'completed' : ''} ${todo.overdue ? 'overdue' : ''}`}><button className={`todo-check ${todo.completed ? 'checked' : ''}`} onClick={async () => { await api.updateTodo(todo.id, { completed: !todo.completed }); await refresh(); }}>{todo.completed ? '✓' : ''}</button><div className="todo-copy">{editing ? <input value={draft} onChange={(event) => setDraft(event.target.value)} autoFocus /> : <b>{todo.text}</b>}<small>{todo.due}</small>{todo.overdue && <small className="overdue-mark">已逾期</small>}</div><div className="todo-actions">{editing ? <><input aria-label="截止时间" type="datetime-local" value={dueAtDraft} onChange={(event) => setDueAtDraft(event.target.value)} /><button onClick={async () => { if (draft.trim()) { await api.updateTodo(todo.id, { text: draft.trim(), due_at: dueAtDraft ? new Date(dueAtDraft).toISOString() : '' }); } setEditingTodo(null); await refresh(); }}>保存</button></> : <button onClick={() => { setDraft(todo.text); setDueAtDraft(toDateTimeLocal(todo.dueAt)); setEditingTodo(todo.id); }}>编辑</button>}<button className="delete-link" onClick={async () => { await api.deleteTodo(todo.id); await refresh(); }}>删除</button></div></div>;
}

export function FeedbackModal({ rating, comment, onRating, onComment, onSubmit, onClose }) {
  const feedbackForm = getFeedbackFormState(rating, comment);
  return <div className="modal-backdrop"><section className="modal feedback-modal" role="dialog" aria-modal="true" aria-labelledby="feedback-modal-title"><button className="modal-close" onClick={onClose} aria-label="关闭意见反馈">×</button><h1 id="feedback-modal-title">意见反馈</h1><p>你的反馈会连同本次音频、完整转写、生成内容和问答保存在本机。</p><div className="rating-row"><button onClick={() => onSubmit('accurate')}>完全准确</button><button className={rating === 'inaccurate' ? 'selected' : ''} onClick={() => onRating('inaccurate')}>内容不准</button></div>{feedbackForm.showDetails && <div className="feedback-details"><textarea required value={comment} onChange={(event) => onComment(event.target.value)} placeholder="请填写具体哪里不准，以及你希望如何改进（必填）" /><button className="primary feedback-submit" disabled={!feedbackForm.canSubmit} onClick={() => onSubmit()}>提交反馈</button></div>}</section></div>;
}

function CardDetail({ card, batch, onClose, onToast }) {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [rating, setRating] = useState('');
  const [comment, setComment] = useState('');
  async function submitFeedback(selectedRating = rating) {
    const submission = getFeedbackFormState(selectedRating, comment);
    if (!submission.canSubmit) return;
    const submittedComment = selectedRating === 'inaccurate' ? comment.trim() : '';
    await api.feedback(card.apiId ?? card.id, selectedRating, submittedComment || null);
    setFeedbackOpen(false); setRating(''); setComment(''); onToast('意见反馈已保存到本地');
  }
  function closeFeedback() {
    setFeedbackOpen(false); setRating(''); setComment('');
  }
  const presentation = card.reportDocument ? null : buildReportEventMap(card.reportMarkdown);
  return <div className="detail-page"><header className="detail-header"><div><span className={`scene-badge ${sceneClass[card.sceneId]}`}>{card.label}</span><h1>{card.title}</h1><p>{batch.date} · {card.timeLabel}</p>{card.reportQuality && <ReportQualityStatus quality={card.reportQuality} />}</div><div className="detail-header-actions"><button className="feedback-trigger" onClick={() => setFeedbackOpen(true)}>意见反馈</button><button className="close-detail" onClick={onClose} aria-label="关闭详情">×</button></div></header><div className="detail-body">{card.reportDocument ? <StructuredReport document={card.reportDocument} /> : card.reportMarkdown ? <>{presentation && <ReportEventMap presentation={presentation} />}<MarkdownReport markdown={card.reportMarkdown} annotations={card.reportAnnotations} omitCoreConclusion={Boolean(presentation)} /></> : <>{card.sceneId === 'analysis' && <section className="analysis-hero"><div className="section-kicker">{card.label} · 核心结论</div><h2>{card.title}</h2><p>{card.summary}</p></section>}{card.detailSections.map((section, index) => ['meeting', 'analysis'].includes(card.sceneId) ? <MeetingDetailSection section={section} key={`${section.title}-${index}`} /> : <section className="detail-section" key={`${section.title}-${index}`}><h2>{section.title}</h2>{section.content && <p>{section.content}</p>}{section.items && <ol>{section.items.map((item) => <li key={item}>{item}</li>)}</ol>}</section>)}</>}{(card.reportDocument || card.reportMarkdown) && <RuntimeMetrics metrics={card.runtimeMetrics} reportMetrics={card.reportMetrics} />}{card.sceneId !== 'analysis' && card.showEvidencePlayback !== false && <EvidencePlayback evidence={card.evidence} />}</div>{feedbackOpen && <FeedbackModal rating={rating} comment={comment} onRating={setRating} onComment={setComment} onSubmit={submitFeedback} onClose={closeFeedback} />}</div>;
}

export function reportQualityLabel(quality) {
  if (!quality) return '';
  const score = Number.isInteger(quality.quality_score) ? `，${quality.quality_score}分` : '';
  if (quality.audit_status === 'completed_unaudited') return '已完成（未审计）';
  if (quality.audit_status === 'completed_v1_revision_failed') return `已完成（V1）${score}`;
  if (quality.audit_status === 'completed_v2_final_audit_degraded') return `已完成（V2），V1审计${quality.quality_score}分`;
  return `已完成${score}`;
}

function ReportQualityStatus({ quality }) {
  return <span className={`report-quality-status ${quality.audit_status}`}>{reportQualityLabel(quality)}</span>;
}

function ReportEventMap({ presentation }) {
  return <section className="report-event-map" aria-labelledby="report-event-map-title"><div className="report-event-map-intro"><h2 id="report-event-map-title">今天发生了什么，重点改进什么</h2><p>{presentation.summary}</p></div><div className="report-event-table-wrap"><table className="report-event-table"><thead><tr><th>阶段</th><th>发生的事</th><th>对应的改进</th></tr></thead><tbody>{presentation.events.map((event) => <tr key={event.kind}><td><strong>{event.phase}</strong></td><td>{event.event}</td><td><strong>{event.improvementTitle}</strong>{event.improvementDetail && <p>{event.improvementDetail}</p>}</td></tr>)}</tbody></table></div></section>;
}

function omitMarkdownSection(markdown, sectionTitle) {
  const lines = String(markdown).replace(/\r\n/g, '\n').split('\n');
  let skipping = false;
  return lines.filter((line) => {
    const heading = /^##\s+(.+)$/.exec(line.trim());
    if (heading) skipping = heading[1].trim() === sectionTitle;
    return !skipping;
  }).join('\n');
}

function StructuredReport({ document }) {
  return <article className="markdown-report structured-report"><section className="report-event-map"><div className="report-event-map-intro"><h2>今天发生了什么，重点改进什么</h2><p>{document.overview.summary}</p></div><div className="report-event-table-wrap"><table className="report-event-table"><thead><tr><th>阶段</th><th>发生的事</th><th>对应的改进</th></tr></thead><tbody>{document.overview.rows.map((row, index) => <tr key={`${row.phase}-${index}`}><td><strong>{row.phase}</strong></td><td>{row.event}</td><td>{row.improvement}</td></tr>)}</tbody></table></div></section><div className="structured-sections">{document.sections.map((section, sectionIndex) => {
    let subsectionIndex = 0;
    return <section className="structured-section" key={`${section.title}-${sectionIndex}`}><h2 className="analysis-section-heading"><span>{sectionIndex + 1}</span>{section.title}</h2><div className="structured-blocks">{section.content ? section.content.split(/\n\s*\n/).filter(Boolean).map((paragraph, paragraphIndex) => <p className="analysis-paragraph" key={paragraphIndex}>{paragraph}</p>) : section.blocks.map((block, blockIndex) => {
      const currentSubsection = ['subsection', 'subheading'].includes(block.type) ? subsectionIndex + 1 : subsectionIndex;
      if (['subsection', 'subheading'].includes(block.type)) subsectionIndex = currentSubsection;
      return <StructuredBlock block={block} sectionNumber={sectionIndex + 1} subsectionNumber={currentSubsection} key={`${block.type}-${blockIndex}`} />;
    })}</div></section>;
  })}</div></article>;
}

function StructuredBlock({ block, sectionNumber, subsectionNumber }) {
  if (block.type === 'paragraph') return <p className="analysis-paragraph">{block.text}</p>;
  if (block.type === 'source_quote') return <blockquote className="analysis-quote structured-source-quote">“{block.text}”</blockquote>;
  if (block.type === 'quote') return <blockquote className="analysis-quote structured-source-quote">“{block.text.replace(/^“|”$/g, '')}”</blockquote>;
  if (block.type === 'suggested_wording') return <blockquote className="analysis-quote structured-suggested-wording">“{block.text}”</blockquote>;
  if (block.type === 'bullet_list') return <ul className="analysis-key-points">{block.items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>;
  if (block.type === 'numbered_list') return <ol className="analysis-insight-grid">{block.items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ol>;
  if (block.type === 'table') return <div className="analysis-matrix-wrap"><table className="analysis-matrix"><thead><tr>{block.columns.map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}</tr></thead><tbody>{block.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div>;
  if (block.type === 'subsection') return <section className="structured-subsection"><h3 className="analysis-subheading"><span>{sectionNumber}.{subsectionNumber}</span>{block.title}</h3><div className="structured-blocks">{block.blocks.map((child, index) => <StructuredBlock block={child} sectionNumber={sectionNumber} subsectionNumber={subsectionNumber} key={`${child.type}-${index}`} />)}</div></section>;
  if (block.type === 'subheading') return <h3 className="analysis-subheading"><span>{sectionNumber}.{subsectionNumber}</span>{block.title}</h3>;
  return null;
}

function MarkdownReport({ markdown = '', annotations = null, omitCoreConclusion = false }) {
  const title = String(markdown).split('\n').find((line) => /^#\s+/.test(line))?.replace(/^#\s+/, '') || '';
  const body = String(markdown).replace(/^#\s+.*(?:\r?\n|$)/, '');
  const reportBody = omitCoreConclusion ? omitMarkdownSection(body, '核心结论') : body;
  return <article className="markdown-report" data-annotation-mode={annotations ? 'model' : 'markdown'}><AnalysisBlocks blocks={analysisBlocks(reportBody, 'full-report', title)} /></article>;
}

function RuntimeMetrics({ metrics, reportMetrics }) {
  if (!metrics && !reportMetrics) return null;
  return <section className="runtime-metrics"><h2>数据范围与运行信息</h2><table className="runtime-metrics-table"><tbody>{reportMetrics?.characterCount != null && <tr><th>本次报告</th><td>{reportMetrics.characterCount} 字</td></tr>}{reportMetrics?.revised && <tr><th>定向修改增益</th><td>{reportMetrics.initialScore} → {reportMetrics.finalScore}（{reportMetrics.gain >= 0 ? '+' : ''}{reportMetrics.gain}）</td></tr>}{reportMetrics && !reportMetrics.revised && reportMetrics.finalScore != null && <tr><th>首次全量审核</th><td>{reportMetrics.finalScore} 分</td></tr>}{metrics && <><tr><th>模型调用</th><td>{metrics.model_call_count ?? 0} 次</td></tr><tr><th>输入 Token</th><td>{metrics.input_tokens ?? 0}</td></tr><tr><th>输出 Token</th><td>{metrics.output_tokens ?? 0}</td></tr><tr><th>联网核验</th><td>{metrics.web_search_performed ? '已进行' : '未进行'}</td></tr></>}</tbody></table></section>;
}

function ExternalSources({ sources = [] }) {
  if (sources.length === 0) return null;
  return <section className="external-sources" aria-labelledby="external-sources-title"><div><h2 id="external-sources-title">外部资料</h2><p>本卡引用的公开资料，与录音回听证据分开展示。</p></div><ul>{sources.map((source) => <li key={source.url}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a>{source.domain && <span>{source.domain}</span>}</li>)}</ul></section>;
}

function Field({ label, children }) {
  if (!children || (Array.isArray(children) && children.length === 0)) return null;
  return <div className="meeting-field"><span>{label}</span>{Array.isArray(children) ? <ul>{children.map((item) => <li key={item}>{item}</li>)}</ul> : <p>{children}</p>}</div>;
}

function MeetingDetailSection({ section }) {
  if (section.kind === 'analysis' || section.kind?.startsWith('autonomous-')) return <AutonomousDetailSection section={section} />;
  if (section.kind === 'overview' || section.kind === 'adaptive') return <section className={`detail-section meeting-section meeting-${section.kind}`}><div className="section-kicker">{section.sectionType || '深度分析'}</div><h2>{section.title}</h2>{section.content && <p>{section.content}</p>}{section.items?.length > 0 && <ul className="meeting-points">{section.items.map((item) => <li key={item}>{item}</li>)}</ul>}</section>;
  if (section.kind === 'participants') return <section className="detail-section meeting-section"><h2>{section.title}</h2><div className="participant-list">{section.entries.map((item) => <span key={`${item.name}-${item.role}`}>{item.name}{item.role && <small>{item.role}</small>}</span>)}</div></section>;
  if (section.kind === 'facts') return <section className="detail-section meeting-section"><h2>{section.title}</h2><div className="meeting-facts">{section.entries.map((item) => <article key={item.fact}><strong>{item.fact}</strong>{item.interpretation && <p>{item.interpretation}</p>}</article>)}</div></section>;
  if (section.kind === 'quotes') return <section className="detail-section meeting-section"><h2>{section.title}</h2>{section.entries.map((item) => <article className="meeting-quote" key={`${item.speaker}-${item.quote}`}><div className="quote-speaker">{item.speaker}</div><blockquote>“{item.quote}”</blockquote><Field label="当时语境">{item.context}</Field><Field label="表层意思">{item.surfaceMeaning}</Field><Field label="深层分析">{item.deeperAnalysis}</Field><Field label="互动影响">{item.interactionEffect}</Field></article>)}</section>;
  if (section.kind === 'arguments') return <section className="detail-section meeting-section"><h2>{section.title}</h2><div className="argument-grid">{section.entries.map((item) => <article className="meeting-argument" key={`${item.speaker}-${item.position}`}><div className="argument-speaker">{item.speaker}</div><h3>{item.position}</h3><Field label="论证逻辑">{item.reasoning}</Field><Field label="事实依据">{item.supportingFacts}</Field><Field label="隐含假设">{item.assumptions}</Field><Field label="对方回应">{item.responseFromOthers}</Field><Field label="反方要点">{item.counterpoints}</Field><Field label="分析判断">{item.assessment}</Field></article>)}</div></section>;
  if (section.kind === 'recommendations') return <section className="detail-section meeting-section"><h2>{section.title}</h2>{section.entries.map((item, index) => <article className="meeting-recommendation" key={`${item.target}-${index}`}><div className="recommendation-target">给 {item.target}</div><h3>{item.recommendation}</h3><Field label="观察到的问题">{item.observedIssue}</Field><Field label="证据依据">{item.evidenceBasis}</Field><Field label="为什么重要">{item.whyItMatters}</Field><Field label="可以这样做">{item.actions}</Field><Field label="可以这样说">{item.suggestedLanguage}</Field><Field label="预期结果">{item.expectedResult}</Field><Field label="注意边界">{item.caveat}</Field></article>)}</section>;
  if (section.kind === 'uncertainties') return <section className="detail-section meeting-section meeting-uncertainties"><h2>{section.title}</h2>{section.entries.map((item) => <article key={item.question}><strong>{item.question}</strong><p>{item.whyUncertain}</p></article>)}</section>;
  return null;
}

function AutonomousDetailSection({ section }) {
  if (section.kind === 'autonomous-finding') {
    const findingLabels = { fact: '明确事实', inference: '分析判断', pattern: '重复模式', strength: '能力优势', risk: '值得警惕', uncertainty: '仍需确认' };
    const confidenceLabels = { high: '高置信度', medium: '中等置信度', low: '低置信度' };
    return <section className="detail-section autonomous-section autonomous-finding"><div className="finding-meta"><span>{findingLabels[section.findingType] || '关键发现'}</span><small>{confidenceLabels[section.confidence] || section.confidence}</small></div>{section.content && <p>{section.content}</p>}</section>;
  }
  if (section.kind === 'autonomous-quotes') return <section className="detail-section autonomous-section"><h2>{section.title}</h2>{section.entries.map((item, index) => <article className="meeting-quote" key={`${item.quote}-${index}`}><blockquote>“{item.quote}”</blockquote><Field label="当时语境">{item.context}</Field><Field label="分析">{item.analysis}</Field></article>)}</section>;
  if (section.kind === 'autonomous-recommendations') return <section className="detail-section autonomous-section analysis-recommendations"><div className="section-kicker">03 · 下一步建议</div><h2>{section.title}</h2><div className="analysis-recommendation-list">{section.entries.map((item, index) => <article className="meeting-recommendation" key={`${item.title}-${index}`}><div className="recommendation-number">{index + 1}</div><div><h3>{item.title}</h3><Field label="为什么提出这条建议">{item.reason}</Field><Field label="可以这样做">{item.actions}</Field><Field label="可以这样说">{item.suggested_language}</Field><Field label="有效的信号">{item.success_signal}</Field><Field label="适用边界">{item.caveat}</Field></div></article>)}</div></section>;
  const isScene = /scene|context|reconstruction|场景|还原/i.test(`${section.sectionType} ${section.title}`);
  return <section className={`detail-section autonomous-section autonomous-editorial ${isScene ? 'editorial-scene' : 'editorial-analysis'}`}><div className="section-kicker">{isScene ? '01 · 场景还原与核心观点' : '02 · 分析、问题与点评'}</div><h2>{section.title}</h2>{section.blocks?.length ? <AnalysisBlocks blocks={section.blocks} /> : section.content && <p>{section.content}</p>}{section.items?.length > 0 && <ul className="meeting-points">{section.items.map((item) => <li key={item}>{item}</li>)}</ul>}</section>;
}

function RichInline({ text = '' }) {
  return String(text).split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map((part, index) => /^\*\*[^*]+\*\*$/.test(part) ? <strong key={index}>{part.slice(2, -2)}</strong> : <span key={index}>{part}</span>);
}

function AnalysisBlocks({ blocks = [] }) {
  let majorSectionNumber = 0;
  let minorSectionNumber = 0;
  return <div className="analysis-blocks">{blocks.map((block, index) => {
    if (block.kind === 'image') return <figure className="report-image" key={index}><img src={block.src} alt={block.alt} loading="lazy" referrerPolicy="no-referrer" />{block.alt && <figcaption>{block.alt}</figcaption>}</figure>;
    if (block.kind === 'quote') return <blockquote className="analysis-quote" key={index}>“{block.text}”</blockquote>;
    if (block.kind === 'divider') return <hr className="analysis-divider" key={index} />;
    if (block.kind === 'heading' && block.level === 2) { majorSectionNumber += 1; minorSectionNumber = 0; return <h2 className="analysis-section-heading" key={index}><span>{majorSectionNumber}</span>{block.text}</h2>; }
    if (block.kind === 'heading' && block.level >= 3) { minorSectionNumber += 1; return <h3 className="analysis-subheading" key={index}><span>{majorSectionNumber}.{minorSectionNumber}</span>{block.text}</h3>; }
    if (block.kind === 'heading') return <h3 className="analysis-subheading" key={index}>{block.text}</h3>;
    if (block.kind === 'bullet-list') return <ul className="analysis-key-points" key={index}>{block.items.map((item) => <li key={item}><RichInline text={item} /></li>)}</ul>;
    if (block.kind === 'timeline') return <ol className="analysis-timeline" key={index}>{block.items.map((item) => <li key={item}><RichInline text={item} /></li>)}</ol>;
    if (block.kind === 'cause-chain') return <div className="analysis-cause-chain" key={index}>{block.items.map((item, itemIndex) => <div className="cause-step" key={`${item}-${itemIndex}`}><span><RichInline text={item} /></span>{itemIndex < block.items.length - 1 && <b aria-hidden="true">→</b>}</div>)}</div>;
    if (block.kind === 'numbered-list') return <ol className="analysis-insight-grid" key={index}>{block.items.map((item, itemIndex) => {
      const structured = typeof item === 'object' && item !== null;
      const text = structured ? item.text : item;
      const continuation = structured ? item.continuation ?? [] : [];
      return <li key={`${structured ? item.ordinal : itemIndex}-${text}`} value={structured ? item.ordinal : undefined}><RichInline text={text} />{continuation.map((paragraph, paragraphIndex) => <p key={`${paragraph}-${paragraphIndex}`}><RichInline text={paragraph} /></p>)}</li>;
    })}</ol>;
    if (block.kind === 'matrix') return <div className="analysis-matrix-wrap" key={index}><table className="analysis-matrix"><thead><tr>{block.rows[0].map((cell, cellIndex) => <th key={cellIndex}>{cell}</th>)}</tr></thead><tbody>{block.rows.slice(1).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}><RichInline text={cell} /></td>)}</tr>)}</tbody></table></div>;
    return <p className="analysis-paragraph" key={index}><RichInline text={block.text} /></p>;
  })}</div>;
}

function evidenceTimeLabel(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function EvidencePlayback({ evidence = [] }) {
  const [active, setActive] = useState(evidence[0] ?? null);
  if (!active || evidence.length === 0) return null;
  const source = `${active.playbackUrl}#t=${(active.startMs / 1000).toFixed(3)},${(active.endMs / 1000).toFixed(3)}`;
  return <details className="evidence-playback" open><summary>回听证据 · {evidence.length} 段</summary><p>选择片段后播放原音频；播放器会从对应时间开始。</p><div className="evidence-segments">{evidence.map((item, index) => <button key={item.segmentId} className={item.segmentId === active.segmentId ? 'active' : ''} aria-pressed={item.segmentId === active.segmentId} onClick={() => setActive(item)}>证据 {index + 1} · {evidenceTimeLabel(item.startMs)}</button>)}</div><audio key={source} controls preload="metadata" src={source}>当前浏览器无法播放这段音频。</audio></details>;
}

function History({ state }) {
  return <main className="page-container"><div className="page-heading"><h1>音频历史</h1><p>已完成分析的音频会自动保存在本机。</p></div>{state.history.length === 0 ? <div className="page-empty"><h2>还没有历史音频</h2><p>完成一次整批分析后，音频会出现在这里。</p></div> : state.history.map((batch) => <section className="history-day" key={batch.id}><div className="date-divider"><b>{batch.date}</b></div><div className="history-batch"><div className="history-batch-title"><b>{batch.uploadedAt} 上传</b><span>{batch.files.length} 个音频</span></div><div className="audio-list">{batch.files.map((file, index) => <div className="audio-row" key={`${file.name}-${index}`}><div className="audio-type">{file.type}</div><div><b>{file.name}</b><span>{file.size} · {file.duration}</span></div><div className="audio-time"><b>{file.time}</b><span>本地文件</span></div></div>)}</div></div></section>)}</main>;
}

function ProviderModal({ state, refresh, onClose, onToast }) {
  const [providerId, setProviderId] = useState(state.activeProvider);
  const [modelId, setModelId] = useState(
    state.providers[state.activeProvider]?.modelName || ''
  );
  const [key, setKey] = useState('');
  const [status, setStatus] = useState({ type: '', message: '' });
  const [checking, setChecking] = useState(false);
  const [cooldownSeconds, setCooldownSeconds] = useState(0);
  const sessionId = useRef(crypto.randomUUID());
  const candidateProviders = useRef(new Set());
  const selected = state.providers[providerId];
  useEffect(() => {
    if (!selected.cooldownUntil) { setCooldownSeconds(0); return undefined; }
    const initialRemaining = Math.max(0, Date.parse(selected.cooldownUntil) - Date.now());
    if (initialRemaining === 0) { setCooldownSeconds(0); refresh().catch(() => {}); return undefined; }
    const monotonicDeadline = performance.now() + initialRemaining;
    let timer;
    const update = () => {
      const remaining = Math.max(0, Math.ceil((monotonicDeadline - performance.now()) / 1000));
      setCooldownSeconds(remaining);
      if (remaining === 0) { clearInterval(timer); refresh().catch(() => {}); }
    };
    update();
    timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [selected.cooldownUntil, refresh]);
  async function submit() {
    setChecking(true); setStatus({ type: '', message: '' });
    candidateProviders.current.add(providerId);
    try {
      await api.saveProviderKey(providerId, key, sessionId.current, modelId);
      candidateProviders.current.delete(providerId);
      await api.activateProvider(providerId);
      await refresh();
      setKey('');
      onToast(`${state.providers[providerId].name} 已配置并设为当前模型`);
      onClose();
    } catch (error) { setStatus({ type: 'error', message: error.message }); }
    finally { setChecking(false); }
  }
  async function revalidate() {
    setChecking(true);
    try { await api.validateProvider(providerId); await refresh(); setStatus({ type: 'success', message: '重新校验成功' }); }
    catch (error) { await refresh(); setStatus({ type: 'error', message: error.message }); }
    finally { setChecking(false); }
  }
  async function activate() {
    await api.activateProvider(providerId); await refresh(); onToast(`已切换到 ${state.providers[providerId].name}`); onClose();
  }
  async function chooseModel(nextModelId) {
    setModelId(nextModelId);
    setStatus({ type: '', message: '' });
    if (!selected.configured || nextModelId === selected.modelName) return;
    setChecking(true);
    try {
      await api.selectProviderModel(providerId, nextModelId);
      await refresh();
      setStatus({ type: 'success', message: '模型已切换并校验成功' });
    } catch (error) {
      setModelId(selected.modelName);
      setStatus({ type: 'error', message: error.message });
    } finally { setChecking(false); }
  }
  async function close() {
    await Promise.all([...candidateProviders.current].map((id) => api.cancelCandidate(id, sessionId.current).catch(() => {})));
    onClose();
  }
  return <div className="modal-backdrop"><section className="modal provider-modal"><button className="modal-close" onClick={close}>×</button><h1>配置分析模型</h1><p>选择厂商和具体模型，再填写 API Key；保存时会立即校验是否可用。</p><div className="provider-tabs">{Object.entries(state.providers).map(([id, provider]) => <button key={id} className={providerId === id ? 'active' : ''} onClick={() => { setProviderId(id); setModelId(provider.modelName || provider.models[0]?.id || ''); setKey(''); setStatus({ type: '', message: '' }); }}>{provider.name}{provider.configured && <small>已配置</small>}</button>)}</div><div className="model-picker"><b>选择具体模型</b><div>{selected.models.map((model) => <button type="button" key={model.id} className={modelId === model.id ? 'active' : ''} disabled={checking} onClick={() => chooseModel(model.id)}><strong>{model.id}</strong><span>{model.label}</span></button>)}</div></div><div className="provider-state-line"><b>{selected.configured ? 'Key 已安全保存' : '尚未配置'}</b><span>{cooldownSeconds > 0 ? `请等待 ${cooldownSeconds} 秒后重试` : selected.error || providerStateLabel[selected.state] || '状态未知'}</span></div><label>API Key<input type="text" value={key} onChange={(event) => setKey(event.target.value)} placeholder={selected.configured ? '已保存，填写新 Key 可覆盖' : `填写 ${selected.name} API Key`} autoFocus autoComplete="off" spellCheck="false" /></label>{status.message && <div className={`validation ${status.type}`}>{status.message}</div>}<div className="modal-actions provider-actions"><button className="secondary" onClick={close}>取消</button>{selected.configured && <button className="secondary" disabled={checking || cooldownSeconds > 0} onClick={revalidate}>{cooldownSeconds > 0 ? `${cooldownSeconds} 秒后重试` : '重新校验'}</button>}{selected.state === 'available' && !selected.active && <button className="secondary" onClick={activate}>设为当前厂商</button>}<button className="primary" disabled={checking || !key.trim() || !modelId} onClick={submit}>{checking ? '正在校验…' : '保存并校验'}</button></div></section></div>;
}

function ClearModal({ onClose, onConfirm }) {
  return <div className="modal-backdrop"><section className="modal clear-modal"><div className="warning-mark">!</div><h1>清除所有历史数据？</h1><p>此操作会永久删除本地历史，无法恢复。删除完成后，首页将回到“先上传音频”。</p><div className="delete-list"><b>将被删除</b><ul><li>所有已上传的音频文件与转写全文</li><li>所有模型生成的卡片、详情和完整问答</li><li>全局待办与由音频建立的个人画像</li></ul></div><div className="keep-note">不会删除：模型厂商与 API Key 配置、Prompt 配置、防休眠设置、已提交的意见反馈。</div><div className="modal-actions"><button className="secondary" onClick={onClose}>取消</button><button className="danger-solid" onClick={onConfirm}>永久清除</button></div></section></div>;
}
