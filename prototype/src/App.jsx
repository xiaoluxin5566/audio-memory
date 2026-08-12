import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createInitialState,
  formatJobEta,
  getFeedbackFormState,
  orderCards,
} from './store.js';
import { api } from './api/client.js';
import { uploadFile } from './api/upload.js';
import { normalizeFeed, normalizeHistory, normalizePrompts } from './api/state.js';
import { useProviders } from './hooks/useProviders.js';
import { useActiveJob } from './hooks/useActiveJob.js';
import { useReanalysis } from './hooks/useReanalysis.js';
import { ReanalysisModal } from './components/ReanalysisModal.jsx';
import { getReanalysisView, isActiveReanalysis } from './api/state.js';
import './styles.css';

const ROUTES = { '/': 'feed', '/history': 'history', '/settings/prompts': 'prompts' };
const ROUTE_PATHS = { feed: '/', history: '/history', prompts: '/settings/prompts' };
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
  const [selectedCard, setSelectedCard] = useState(null);
  const [toast, setToast] = useState('');
  const [editingTodo, setEditingTodo] = useState(null);
  const [promptScene, setPromptScene] = useState('autonomous-analysis');
  const [promptDraft, setPromptDraft] = useState('');
  const fileInput = useRef(null);
  const pendingUploadFiles = useRef([]);
  const lastFinishedReanalysis = useRef(null);
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

  const refreshPrompts = useCallback(async () => {
    const prompts = normalizePrompts(await api.prompts());
    setState((current) => ({ ...current, prompts: { ...current.prompts, ...prompts } }));
    setPromptScene((current) => prompts[current] ? current : Object.keys(prompts)[0] || current);
    setPromptDraft((current) => current || prompts['autonomous-analysis']?.current || Object.values(prompts)[0]?.current || '');
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
    refreshPrompts().catch(() => setToast('Prompt 加载失败'));
  }, [refreshContent, refreshPrompts]);
  useEffect(() => {
    api.activeJob().then((job) => {
      if (!job) return;
      setState((current) => ({
        ...current,
        job: { ...job, progress: job.progress_percent ?? 0 },
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

  async function startAnalysis() {
    const provider = state.providers[state.activeProvider];
    if (provider?.state !== 'available') { setProviderOpen(true); return; }
    if (!state.upload.files.length || state.upload.paused || state.upload.files.some((file) => file.progress < 100)) return;
    const job = await api.startJob(state.job.id);
    setState((current) => ({ ...current, job: { ...job, progress: job.progress_percent ?? 0 } }));
  }

  const onJobUpdate = useCallback((job) => {
    const progress = job.progress_percent ?? 0;
    setState((current) => ({ ...current, job: { ...current.job, ...job, progress } }));
  }, []);
  const onJobComplete = useCallback(async () => {
    await refreshContent();
    setState((current) => ({ ...current, upload: { files: [], error: '', paused: false }, job: null }));
    setToast('分析完成，新结果已添加到信息流');
  }, [refreshContent]);
  const watchedJobId = ['transcribing', 'analyzing'].includes(state.job?.stage)
    ? state.job.id
    : null;
  useActiveJob(watchedJobId, onJobUpdate, onJobComplete);

  async function resumeJob() {
    if (state.job.stage === 'failed') {
      await api.retryAnalysis(state.job.id);
      setState((current) => ({ ...current, job: { ...current.job, stage: 'analyzing', progress: 70, error_code: null } }));
      return;
    }
    await api.resumeJob(state.job.id);
    setState((current) => ({ ...current, job: { ...current.job, stage: 'transcribing', progress: 0 } }));
  }
  async function cancelJob() {
    if (state.job?.id) await api.cancelJob(state.job.id);
    setState((current) => ({ ...current, job: null, upload: { files: [], error: '', paused: false } }));
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
            <section className="panel upload-panel">
              <div className="panel-title"><strong>上传音频</strong><span>{state.upload.files.length ? `${state.upload.files.length} 个文件` : ''}</span></div>
              <div className={`drop-zone ${currentProvider.state !== 'available' ? 'disabled' : ''}`} onClick={() => currentProvider.state === 'available' ? fileInput.current?.click() : setProviderOpen(true)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); addFiles(event.dataTransfer.files); }}>
                <b>拖拽音频到这里，或点击选择</b><span>支持 MP3、AAC</span>
                <input ref={fileInput} type="file" multiple disabled={currentProvider.state !== 'available'} accept=".mp3,.aac,audio/mpeg,audio/aac" onChange={(event) => { addFiles(event.target.files); event.target.value = ''; }} />
              </div>
              {state.upload.error && <div className="inline-error"><b>{state.upload.error}</b><span>移除不支持的文件后可继续。</span></div>}
              <div className="file-stack">{state.upload.files.map((file) => <UploadFile key={file.id} file={file} onRemove={() => removeFile(file.id)} />)}</div>
              {state.job && state.job.stage !== 'uploading' ? <JobPanel job={state.job} onRetry={resumeJob} onCancel={cancelJob} /> : (
                <button className="primary full" disabled={!state.upload.files.length || state.upload.paused || state.upload.files.some((file) => file.progress < 100)} onClick={startAnalysis}>开始分析{state.upload.files.length ? ` ${state.upload.files.length} 个文件` : ''}</button>
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
      {route === 'prompts' && <PromptSettings state={state} scene={promptScene} setScene={(id) => { setPromptScene(id); setPromptDraft(state.prompts[id]?.current || ''); }} draft={promptDraft} />}
      {providerOpen && <ProviderModal state={state} refresh={providerState.refresh} onClose={() => setProviderOpen(false)} onToast={setToast} />}
      {reanalysisOpen && <ReanalysisModal preview={reanalysis.preview} loading={reanalysis.loadingPreview} error={reanalysis.error} current={reanalysis.current} view={reanalysisView} onClose={closeReanalysis} onConfirm={confirmReanalysis} onAction={controlReanalysis} />}
      {clearOpen && <ClearModal onClose={() => setClearOpen(false)} onConfirm={async () => { await api.clearHistory(); reanalysis.clearState(); setState((current) => ({ ...current, feed: [], todos: [], history: [] })); await refreshContent(); setSelectedCard(null); setClearOpen(false); setToast('所有历史已清除'); navigate('feed'); }} />}
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}

function Topbar({ route, onNavigate, reanalysis, onReanalyze, onClear }) {
  return <header className="topbar"><div className="brand"><div className="brand-mark">AM</div><div><b>Audio Memory</b><span>本地音频智能分析</span></div></div><div className="top-actions"><nav>{[['feed', '信息流'], ['history', '音频历史'], ['prompts', 'Prompt 设置']].map(([id, label]) => <button key={id} className={route === id ? 'active' : ''} onClick={() => onNavigate(id)}>{label}</button>)}</nav><button className="secondary reanalysis-entry" disabled={reanalysis.state === 'disabled' || reanalysis.state === 'stopping'} onClick={onReanalyze}>{reanalysis.buttonLabel}</button><button className="danger-ghost" disabled={!reanalysis.canClearHistory} onClick={onClear}>清除所有历史</button></div></header>;
}

function UploadFile({ file, onRemove }) {
  return <div className={`upload-file ${file.invalid ? 'invalid' : ''}`}><div className="file-type">{file.type}</div><div className="file-main"><b>{file.name}</b><span>{prettySize(file.size)} · {file.invalid ? '不支持的格式' : file.progress === 100 ? '上传完成' : `上传中 ${file.progress}%`}</span>{!file.invalid && file.progress < 100 && <div className="progress"><i style={{ width: `${file.progress}%` }} /></div>}</div><button className="icon-button" onClick={onRemove} aria-label={`移除 ${file.name}`}>×</button></div>;
}

function JobPanel({ job, onRetry, onCancel }) {
  if (job.stage === 'interrupted') return <div className="job-card warning"><b>发现未完成的分析任务</b><p>上次处理在中断前已保存进度，可以从中断位置继续。</p><div><button className="secondary" onClick={onCancel}>取消任务</button><button className="primary" onClick={onRetry}>继续分析</button></div></div>;
  if (job.stage === 'failed') return <div className="job-card error"><b>模型分析失败</b>{job.error_code && <code>{job.error_code}</code>}<p>已保留完整转写；可修改当前厂商后重新分析，不会再次执行 Whisper。</p><div><button className="secondary" onClick={onCancel}>放弃任务</button><button className="primary" onClick={onRetry}>重新分析</button></div></div>;
  const transcribing = job.stage === 'transcribing';
  const phase = transcribing ? `${job.local_phase || '准备本地转写'}${job.batch_total ? ` ${job.batch_current}/${job.batch_total}` : ''}` : '生成深度分析';
  return <div className="job-card"><div className="job-title"><b>{phase}</b><span>{job.progress}%</span></div><div className="progress large"><i style={{ width: `${job.progress}%` }} /></div><p className="job-eta">{formatJobEta(job)}</p>{transcribing && <p>快速转写（Beta）可能遗漏低音量、远场或重叠语音，也可能把背景媒体识别为对话。关键人物、数字、日期和待办请回听原音频确认。</p>}<div className="stage-row done"><i />音频上传<span>已完成</span></div><div className={`stage-row ${transcribing ? 'doing' : 'done'}`}><i />本地转写与时间轴校验<span>{transcribing ? '进行中' : '已完成'}</span></div><div className={`stage-row ${transcribing ? 'waiting' : 'doing'}`}><i />自主分析、隐藏画像与发布<span>{transcribing ? '等待中' : '进行中'}</span></div><button className="secondary full" onClick={onCancel}>取消本次分析</button></div>;
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
  const [question, setQuestion] = useState('');
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [rating, setRating] = useState('');
  const [comment, setComment] = useState('');
  const [qa, setQa] = useState(batch.qa?.[card.id] ?? []);
  async function ask() {
    if (!question.trim()) return;
    const response = await api.askCard(card.apiId ?? card.id, question.trim());
    const pairs = [];
    for (let index = 0; index < response.messages.length; index += 2) {
      const user = response.messages[index];
      const assistant = response.messages[index + 1];
      if (user?.role === 'user' && assistant?.role === 'assistant') pairs.push({ q: user.content, a: assistant.content });
    }
    setQa(pairs);
    setQuestion('');
  }
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
  return <div className="detail-page"><header className="detail-header"><div><span className={`scene-badge ${sceneClass[card.sceneId]}`}>{card.label}</span><h1>{card.title}</h1><p>{batch.date} · {card.timeLabel}</p></div><div className="detail-header-actions"><button className="feedback-trigger" onClick={() => setFeedbackOpen(true)}>意见反馈</button><button className="close-detail" onClick={onClose} aria-label="关闭详情">×</button></div></header><div className="detail-body">{card.sceneId === 'analysis' && <section className="analysis-hero"><div className="section-kicker">{card.label} · 核心结论</div><h2>{card.title}</h2><p>{card.summary}</p></section>}{card.detailSections.map((section, index) => ['meeting', 'analysis'].includes(card.sceneId) ? <MeetingDetailSection section={section} key={`${section.title}-${index}`} /> : <section className="detail-section" key={`${section.title}-${index}`}><h2>{section.title}</h2>{section.content && <p>{section.content}</p>}{section.items && <ol>{section.items.map((item) => <li key={item}>{item}</li>)}</ol>}</section>)}<ExternalSources sources={card.sources} />{card.sceneId !== 'analysis' && card.showEvidencePlayback !== false && <EvidencePlayback evidence={card.evidence} />}{qa.length > 0 && <section className="qa-section"><h2>对话记录</h2>{qa.map((item, index) => <div className="qa-pair" key={`${item.q}-${index}`}><div className="chat-message user"><div className="chat-bubble">{item.q}</div></div><div className="chat-message assistant"><div className="chat-bubble">{item.a}</div></div></div>)}</section>}<section className="ask-section"><h2>继续追问</h2><p>仅围绕当前{card.label}内容回答。</p><div className="ask-box"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：帮我把最关键的下一步说得更具体" /><button className="primary" onClick={ask}>发送</button></div></section></div>{feedbackOpen && <FeedbackModal rating={rating} comment={comment} onRating={setRating} onComment={setComment} onSubmit={submitFeedback} onClose={closeFeedback} />}</div>;
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
  return <div className="analysis-blocks">{blocks.map((block, index) => {
    if (block.kind === 'heading') return <h3 className="analysis-subheading" key={index}>{block.text}</h3>;
    if (block.kind === 'bullet-list') return <ul className="analysis-key-points" key={index}>{block.items.map((item) => <li key={item}><RichInline text={item} /></li>)}</ul>;
    if (block.kind === 'timeline') return <ol className="analysis-timeline" key={index}>{block.items.map((item) => <li key={item}><RichInline text={item} /></li>)}</ol>;
    if (block.kind === 'cause-chain') return <div className="analysis-cause-chain" key={index}>{block.items.map((item, itemIndex) => <div className="cause-step" key={`${item}-${itemIndex}`}><span><RichInline text={item} /></span>{itemIndex < block.items.length - 1 && <b aria-hidden="true">→</b>}</div>)}</div>;
    if (block.kind === 'numbered-list') return <div className="analysis-insight-grid" key={index}>{block.items.map((item, itemIndex) => { const parts = item.split(/[：:]/); const label = parts.length > 1 ? parts.shift() : `${itemIndex + 1}`; return <article key={item}><span>{String(itemIndex + 1).padStart(2, '0')}</span><h4>{label}</h4><p><RichInline text={parts.length ? parts.join('：') : item} /></p></article>; })}</div>;
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

function PromptSettings({ state, scene, setScene, draft }) {
  const prompts = Object.entries(state.prompts).filter(([, prompt]) => prompt.source === 'versioned-code');
  const active = state.prompts[scene] ?? prompts[0]?.[1];
  return <main className="page-container prompt-page"><div className="page-heading"><h1>Prompt 设置</h1><p>这里展示当前分析流程实际使用的生产 Prompt。</p></div><div className="prompt-workspace"><aside><h2>生产 Prompt</h2>{prompts.map(([id, prompt]) => <button key={id} className={scene === id ? 'active' : ''} onClick={() => setScene(id)}><span>{prompt.label}</span><small>v{prompt.version}</small></button>)}</aside><section className="prompt-editor"><div className="prompt-editor-head"><div><h2>{active?.label ?? '正在加载'}</h2><p>版本化来源：自主分析规范</p></div></div><div className="prompt-info">当前生产 Prompt，由程序版本化维护。修改需通过代码、测试与重新发布；旧六场景仅保留历史兼容，不参与新分析。</div><textarea className="prompt-textarea" readOnly value={draft} /><div className="prompt-actions"><span>当前为只读状态</span></div></section></div></main>;
}

function ProviderModal({ state, refresh, onClose, onToast }) {
  const [providerId, setProviderId] = useState(state.activeProvider);
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
      await api.saveProviderKey(providerId, key, sessionId.current);
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
  async function close() {
    await Promise.all([...candidateProviders.current].map((id) => api.cancelCandidate(id, sessionId.current).catch(() => {})));
    onClose();
  }
  return <div className="modal-backdrop"><section className="modal provider-modal"><button className="modal-close" onClick={close}>×</button><h1>配置分析模型</h1><p>选择预制厂商并填写 API Key，保存时会立即校验是否可用。</p><div className="provider-tabs">{Object.entries(state.providers).map(([id, provider]) => <button key={id} className={providerId === id ? 'active' : ''} onClick={() => { setProviderId(id); setKey(''); setStatus({ type: '', message: '' }); }}>{provider.name}{provider.configured && <small>已配置</small>}</button>)}</div><div className="provider-state-line"><b>{selected.configured ? 'Key 已安全保存' : '尚未配置'}</b><span>{cooldownSeconds > 0 ? `请等待 ${cooldownSeconds} 秒后重试` : selected.error || providerStateLabel[selected.state] || '状态未知'}</span></div><label>API Key<input type="text" value={key} onChange={(event) => setKey(event.target.value)} placeholder={selected.configured ? '已保存，填写新 Key 可覆盖' : `填写 ${selected.name} API Key`} autoFocus autoComplete="off" spellCheck="false" /></label>{status.message && <div className={`validation ${status.type}`}>{status.message}</div>}<div className="modal-actions provider-actions"><button className="secondary" onClick={close}>取消</button>{selected.configured && <button className="secondary" disabled={checking || cooldownSeconds > 0} onClick={revalidate}>{cooldownSeconds > 0 ? `${cooldownSeconds} 秒后重试` : '重新校验'}</button>}{selected.state === 'available' && !selected.active && <button className="secondary" onClick={activate}>设为当前厂商</button>}<button className="primary" disabled={checking || !key.trim()} onClick={submit}>{checking ? '正在校验…' : '保存并校验'}</button></div></section></div>;
}

function ClearModal({ onClose, onConfirm }) {
  return <div className="modal-backdrop"><section className="modal clear-modal"><div className="warning-mark">!</div><h1>清除所有历史数据？</h1><p>此操作会永久删除本地历史，无法恢复。删除完成后，首页将回到“先上传音频”。</p><div className="delete-list"><b>将被删除</b><ul><li>所有已上传的音频文件与转写全文</li><li>所有模型生成的卡片、详情和完整问答</li><li>全局待办与由音频建立的个人画像</li></ul></div><div className="keep-note">不会删除：模型厂商与 API Key 配置、Prompt 配置、已提交的意见反馈。</div><div className="modal-actions"><button className="secondary" onClick={onClose}>取消</button><button className="danger-solid" onClick={onConfirm}>永久清除</button></div></section></div>;
}
