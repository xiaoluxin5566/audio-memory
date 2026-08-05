import { useEffect, useMemo, useRef, useState } from 'react';
import { SCENES } from './defaults.js';
import { acceptAudioFile, buildMockBatch, validateProviderKey } from './mockEngine.js';
import {
  appendCardQA,
  clearHistoryLayers,
  createFeedbackRecord,
  createInitialState,
  getFeedbackFormState,
  loadState,
  orderCards,
  savePromptRevision,
  saveState,
} from './store.js';
import './styles.css';

const ROUTES = { '/': 'feed', '/history': 'history', '/settings/prompts': 'prompts' };
const ROUTE_PATHS = { feed: '/', history: '/history', prompts: '/settings/prompts' };
const sceneClass = { meeting: 'meeting', parenting: 'parenting', content: 'content', growth: 'growth', inspiration: 'inspiration' };

function prettySize(bytes = 0) {
  if (!bytes) return '模拟文件';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function initialProductState() {
  const saved = loadState();
  if (saved.job && ['uploading', 'transcribing', 'analyzing'].includes(saved.job.stage)) {
    saved.job = { ...saved.job, previousStage: saved.job.stage, stage: 'interrupted' };
  }
  return saved;
}

export function App() {
  const [state, setState] = useState(initialProductState);
  const [route, setRoute] = useState(ROUTES[window.location.pathname] ?? 'feed');
  const [providerOpen, setProviderOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [selectedCard, setSelectedCard] = useState(null);
  const [toast, setToast] = useState('');
  const [editingTodo, setEditingTodo] = useState(null);
  const [promptScene, setPromptScene] = useState('todo');
  const [promptEditing, setPromptEditing] = useState(false);
  const [promptDraft, setPromptDraft] = useState(state.prompts.todo.current);
  const fileInput = useRef(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => saveState(localStorage, state), [state]);
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

  function update(mutator) {
    setState((current) => {
      const next = structuredClone(current);
      mutator(next);
      return next;
    });
  }

  function navigate(nextRoute) {
    setSelectedCard(null);
    setRoute(nextRoute);
    window.history.pushState({}, '', ROUTE_PATHS[nextRoute]);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function addFiles(fileList) {
    if (!state.providers[state.activeProvider].configured) {
      setProviderOpen(true);
      return;
    }
    const files = [...fileList];
    if (!files.length) return;
    const rejected = files.find((file) => !acceptAudioFile(file).ok);
    if (rejected) {
      update((next) => {
        next.upload.error = acceptAudioFile(rejected).error;
        next.upload.paused = true;
        next.upload.files.push({ id: crypto.randomUUID(), name: rejected.name, size: rejected.size, type: 'UNSUPPORTED', progress: 0, invalid: true });
      });
      return;
    }
    const added = files.map((file) => ({
      id: crypto.randomUUID(), name: file.name, size: file.size, type: acceptAudioFile(file).extension, progress: 0, invalid: false,
    }));
    update((next) => { next.upload.files.push(...added); next.upload.error = ''; next.upload.paused = false; });
    added.forEach((item, index) => {
      let progress = 0;
      const timer = setInterval(() => {
        progress = Math.min(100, progress + 20);
        update((next) => {
          const target = next.upload.files.find((file) => file.id === item.id);
          if (target) target.progress = progress;
        });
        if (progress === 100) clearInterval(timer);
      }, 120 + index * 40);
    });
  }

  function removeFile(id) {
    update((next) => {
      next.upload.files = next.upload.files.filter((file) => file.id !== id);
      if (!next.upload.files.some((file) => file.invalid)) { next.upload.error = ''; next.upload.paused = false; }
    });
  }

  function startAnalysis() {
    const provider = state.providers[state.activeProvider];
    if (!provider.configured) { setProviderOpen(true); return; }
    if (!state.upload.files.length || state.upload.paused || state.upload.files.some((file) => file.progress < 100)) return;
    update((next) => {
      next.job = { stage: 'transcribing', progress: 0, completedStages: ['uploading'], files: structuredClone(next.upload.files), error: '' };
    });
  }

  useEffect(() => {
    if (!state.job || !['transcribing', 'analyzing'].includes(state.job.stage)) return undefined;
    const stage = state.job.stage;
    const timer = setInterval(() => {
      setState((current) => {
        if (!current.job || current.job.stage !== stage) return current;
        const next = structuredClone(current);
        next.job.progress = Math.min(100, next.job.progress + 10);
        const failToken = stage === 'transcribing' ? 'transcription-fail' : 'analysis-fail';
        if (!next.job.retrying && next.job.progress >= 50 && next.job.files.some((file) => file.name.toLowerCase().includes(failToken))) {
          next.job = { ...next.job, failedStage: stage, stage: 'failed', error: stage === 'transcribing' ? '本地转写失败，请重试' : '模型分析失败，请重试' };
          return next;
        }
        if (next.job.progress === 100 && stage === 'transcribing') {
          next.job = { ...next.job, stage: 'analyzing', progress: 0, completedStages: [...next.job.completedStages, 'transcribing'] };
        } else if (next.job.progress === 100 && stage === 'analyzing') {
          const batch = buildMockBatch(next.job.files, next.activeProvider, next.prompts);
          batch.qa = {};
          next.feed.unshift(batch);
          next.history.unshift({
            id: `history-${batch.id}`, date: batch.date, uploadedAt: batch.uploadedAt,
            files: next.job.files.map((file) => ({ name: file.name, type: file.type, size: prettySize(file.size), duration: '时长已识别', time: batch.uploadedAt })),
          });
          next.todos.unshift({ id: `todo-${Date.now()}`, text: '下周二前完成可交互原型', due: '下周二', overdue: false, completed: false });
          next.hiddenProfile = { interests: ['AI 产品', '结构化表达', '亲子教育'], updatedAt: Date.now() };
          next.upload = { files: [], error: '', paused: false };
          next.job = null;
          setToast('分析完成，新结果已添加到信息流');
        }
        return next;
      });
    }, 220);
    return () => clearInterval(timer);
  }, [state.job?.stage]);

  function resumeJob() {
    update((next) => { next.job.stage = next.job.previousStage || next.job.failedStage || 'transcribing'; next.job.progress = Math.min(next.job.progress, 40); next.job.error = ''; next.job.retrying = true; });
  }
  function cancelJob() { update((next) => { next.job = null; next.upload = { files: [], error: '', paused: false }; }); }

  const currentProvider = state.providers[state.activeProvider];
  return (
    <div className="app-shell">
      <Topbar route={route} onNavigate={navigate} onClear={() => setClearOpen(true)} />
      {route === 'feed' && (
        <div className="home-layout">
          <aside className="control-rail">
            <h1>分析音频</h1>
            <section className="panel provider-panel">
              <div className="panel-title"><strong>模型与 API Key</strong><span className={currentProvider.configured ? 'ok-text' : ''}>{currentProvider.configured ? '已配置' : '未配置'}</span></div>
              <div className="provider-summary"><div><b>{currentProvider.name}</b><small>{currentProvider.configured ? '用于内容分析' : '请先完成配置'}</small></div><button className="secondary compact" onClick={() => setProviderOpen(true)}>{currentProvider.configured ? '修改' : '去配置'}</button></div>
              {currentProvider.configured && <div className="status-success"><i />连接可用 · {currentProvider.lastChecked} 校验</div>}
            </section>
            <section className="panel upload-panel">
              <div className="panel-title"><strong>上传音频</strong><span>{state.upload.files.length ? `${state.upload.files.length} 个文件` : ''}</span></div>
              <div className={`drop-zone ${!currentProvider.configured ? 'disabled' : ''}`} onClick={() => currentProvider.configured ? fileInput.current?.click() : setProviderOpen(true)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); addFiles(event.dataTransfer.files); }}>
                <b>拖拽音频到这里，或点击选择</b><span>支持 MP3、AAC</span>
                <input ref={fileInput} type="file" multiple disabled={!currentProvider.configured} accept=".mp3,.aac,audio/mpeg,audio/aac" onChange={(event) => { addFiles(event.target.files); event.target.value = ''; }} />
              </div>
              {state.upload.error && <div className="inline-error"><b>{state.upload.error}</b><span>移除不支持的文件后可继续。</span></div>}
              <div className="file-stack">{state.upload.files.map((file) => <UploadFile key={file.id} file={file} onRemove={() => removeFile(file.id)} />)}</div>
              {state.job ? <JobPanel job={state.job} onRetry={resumeJob} onCancel={cancelJob} /> : (
                <button className="primary full" disabled={!state.upload.files.length || state.upload.paused || state.upload.files.some((file) => file.progress < 100)} onClick={startAnalysis}>开始分析{state.upload.files.length ? ` ${state.upload.files.length} 个文件` : ''}</button>
              )}
              <p className="privacy">音频、转写和结果保存在本机；只有转写文本会发送给当前模型厂商。</p>
            </section>
          </aside>
          <main className="feed-area">
            {selectedCard ? <CardDetail card={selectedCard.card} batch={selectedCard.batch} state={state} update={update} onClose={() => setSelectedCard(null)} onToast={setToast} /> : <Feed state={state} update={update} editingTodo={editingTodo} setEditingTodo={setEditingTodo} onOpenCard={(card, batch) => setSelectedCard({ card, batch })} />}
          </main>
        </div>
      )}
      {route === 'history' && <History state={state} />}
      {route === 'prompts' && <PromptSettings state={state} update={update} scene={promptScene} setScene={(id) => { setPromptScene(id); setPromptDraft(state.prompts[id].current); setPromptEditing(false); }} draft={promptDraft} setDraft={setPromptDraft} editing={promptEditing} setEditing={setPromptEditing} onToast={setToast} />}
      {providerOpen && <ProviderModal state={state} update={update} onClose={() => setProviderOpen(false)} onToast={setToast} />}
      {clearOpen && <ClearModal onClose={() => setClearOpen(false)} onConfirm={() => { setState((current) => clearHistoryLayers(current)); setSelectedCard(null); setClearOpen(false); setToast('所有历史已清除'); navigate('feed'); }} />}
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}

function Topbar({ route, onNavigate, onClear }) {
  return <header className="topbar"><div className="brand"><div className="brand-mark">AM</div><div><b>Audio Memory</b><span>本地音频智能分析</span></div></div><div className="top-actions"><nav>{[['feed', '信息流'], ['history', '音频历史'], ['prompts', 'Prompt 设置']].map(([id, label]) => <button key={id} className={route === id ? 'active' : ''} onClick={() => onNavigate(id)}>{label}</button>)}</nav><button className="danger-ghost" onClick={onClear}>清除所有历史</button></div></header>;
}

function UploadFile({ file, onRemove }) {
  return <div className={`upload-file ${file.invalid ? 'invalid' : ''}`}><div className="file-type">{file.type}</div><div className="file-main"><b>{file.name}</b><span>{prettySize(file.size)} · {file.invalid ? '不支持的格式' : file.progress === 100 ? '上传完成' : `上传中 ${file.progress}%`}</span>{!file.invalid && file.progress < 100 && <div className="progress"><i style={{ width: `${file.progress}%` }} /></div>}</div><button className="icon-button" onClick={onRemove} aria-label={`移除 ${file.name}`}>×</button></div>;
}

function JobPanel({ job, onRetry, onCancel }) {
  if (job.stage === 'interrupted') return <div className="job-card warning"><b>发现未完成的分析任务</b><p>上次处理在中断前已保存进度，可以从中断位置继续。</p><div><button className="secondary" onClick={onCancel}>取消任务</button><button className="primary" onClick={onRetry}>继续分析</button></div></div>;
  if (job.stage === 'failed') return <div className="job-card error"><b>{job.error}</b><p>已完成的阶段不会重复执行。</p><div><button className="secondary" onClick={onCancel}>取消</button><button className="primary" onClick={onRetry}>重试</button></div></div>;
  const transcribing = job.stage === 'transcribing';
  return <div className="job-card"><div className="job-title"><b>{transcribing ? '本地 Whisper 转写中' : '模型正在分析内容'}</b><span>{job.progress}%</span></div><div className="progress large"><i style={{ width: `${job.progress}%` }} /></div><div className="stage-row done"><i />音频上传<span>已完成</span></div><div className={`stage-row ${transcribing ? 'doing' : 'done'}`}><i />Whisper 转写<span>{transcribing ? '进行中' : '已完成'}</span></div><div className={`stage-row ${transcribing ? 'waiting' : 'doing'}`}><i />场景分析与结果生成<span>{transcribing ? '等待中' : '进行中'}</span></div><button className="secondary full" onClick={onCancel}>取消本次分析</button></div>;
}

function Feed({ state, update, editingTodo, setEditingTodo, onOpenCard }) {
  if (!state.feed.length && !state.todos.length) return <div className="empty-feed"><div className="empty-mark">AM</div><h2>先上传音频</h2><p>分析完成后，待办、会议、家庭教育、内容推荐与成长建议会出现在这里。</p></div>;
  const incomplete = state.todos.filter((todo) => !todo.completed);
  const completed = state.todos.filter((todo) => todo.completed);
  return <div className="feed-content">{state.todos.length > 0 && <section className="todo-card"><div className="todo-head"><h2>全局待办</h2><span>{incomplete.length} 项未完成</span></div>{incomplete.map((todo) => <TodoRow key={todo.id} todo={todo} update={update} editingTodo={editingTodo} setEditingTodo={setEditingTodo} />)}{completed.length > 0 && <><div className="completed-label">已完成 · {completed.length}</div>{completed.map((todo) => <TodoRow key={todo.id} todo={todo} update={update} editingTodo={editingTodo} setEditingTodo={setEditingTodo} />)}</>}</section>}{state.feed.map((batch) => <section className="day-block" key={batch.id}><div className="date-divider"><b>{batch.date}</b><span>最新更新 {batch.uploadedAt}</span></div><div className="batch-line"><div className="batch-title"><b>{batch.uploadedAt} 上传 · {batch.audio.length} 个音频</b><span>分析完成</span></div>{orderCards(batch.cards).map((card) => <article className="result-card" key={card.id} onClick={() => onOpenCard(card, batch)}><div className="result-head"><span className={`scene-badge ${sceneClass[card.sceneId]}`}>{card.label}</span><small>{card.timeLabel}</small></div><h3>{card.title}</h3><p>{card.summary}</p><div className="result-foot"><span>{card.meta}</span><button>查看完整结果 ›</button></div></article>)}</div></section>)}</div>;
}

function TodoRow({ todo, update, editingTodo, setEditingTodo }) {
  const [draft, setDraft] = useState(todo.text);
  const editing = editingTodo === todo.id;
  return <div className={`todo-row ${todo.completed ? 'completed' : ''} ${todo.overdue ? 'overdue' : ''}`}><button className={`todo-check ${todo.completed ? 'checked' : ''}`} onClick={() => update((next) => { next.todos.find((item) => item.id === todo.id).completed = !todo.completed; })}>{todo.completed ? '✓' : ''}</button><div className="todo-copy">{editing ? <input value={draft} onChange={(event) => setDraft(event.target.value)} autoFocus /> : <b>{todo.text}</b>}<small>{todo.due}</small></div><div className="todo-actions">{editing ? <button onClick={() => { update((next) => { next.todos.find((item) => item.id === todo.id).text = draft.trim() || todo.text; }); setEditingTodo(null); }}>保存</button> : <button onClick={() => { setDraft(todo.text); setEditingTodo(todo.id); }}>编辑</button>}<button className="delete-link" onClick={() => update((next) => { next.todos = next.todos.filter((item) => item.id !== todo.id); })}>删除</button></div></div>;
}

export function FeedbackModal({ rating, comment, onRating, onComment, onSubmit, onClose }) {
  const feedbackForm = getFeedbackFormState(rating, comment);
  return <div className="modal-backdrop"><section className="modal feedback-modal" role="dialog" aria-modal="true" aria-labelledby="feedback-modal-title"><button className="modal-close" onClick={onClose} aria-label="关闭意见反馈">×</button><h1 id="feedback-modal-title">意见反馈</h1><p>你的反馈会连同本次音频、完整转写、生成内容和问答保存在本机。</p><div className="rating-row"><button onClick={() => onSubmit('accurate')}>完全准确</button><button className={rating === 'inaccurate' ? 'selected' : ''} onClick={() => onRating('inaccurate')}>内容不准</button></div>{feedbackForm.showDetails && <div className="feedback-details"><textarea required value={comment} onChange={(event) => onComment(event.target.value)} placeholder="请填写具体哪里不准，以及你希望如何改进（必填）" /><button className="primary feedback-submit" disabled={!feedbackForm.canSubmit} onClick={() => onSubmit()}>提交反馈</button></div>}</section></div>;
}

function CardDetail({ card, batch, state, update, onClose, onToast }) {
  const [question, setQuestion] = useState('');
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [rating, setRating] = useState('');
  const [comment, setComment] = useState('');
  const [qa, setQa] = useState(batch.qa?.[card.id] ?? []);
  function ask() {
    if (!question.trim()) return;
    const answer = card.sceneId === 'meeting' ? '根据当前会议内容，最关键的下一步是在下周二前完成可交互原型，并用同一批音频验证三个模型的生成效果。' : '结合这张卡片的完整内容，建议先选择一个最容易执行的小步骤，完成后再根据实际反馈调整。';
    const pair = { q: question.trim(), a: answer };
    update((next) => { const target = next.feed.find((item) => item.id === batch.id); appendCardQA(target, card.id, pair); });
    setQa((current) => [...current, pair]);
    setQuestion('');
  }
  function submitFeedback(selectedRating = rating) {
    const submission = getFeedbackFormState(selectedRating, comment);
    if (!submission.canSubmit) return;
    const submittedComment = selectedRating === 'inaccurate' ? comment.trim() : '';
    update((next) => { next.feedback.push(createFeedbackRecord({ sceneId: card.sceneId, audio: batch.audio, transcript: batch.transcript, generatedContent: card, promptVersion: batch.promptVersions?.[card.sceneId] ?? 1, rating: selectedRating, comment: submittedComment, qa })); });
    setFeedbackOpen(false); setRating(''); setComment(''); onToast('意见反馈已保存到本地');
  }
  function closeFeedback() {
    setFeedbackOpen(false); setRating(''); setComment('');
  }
  return <div className="detail-page"><header className="detail-header"><div><span className={`scene-badge ${sceneClass[card.sceneId]}`}>{card.label}</span><h1>{card.title}</h1><p>{batch.date} · {card.timeLabel}</p></div><div className="detail-header-actions"><button className="feedback-trigger" onClick={() => setFeedbackOpen(true)}>意见反馈</button><button className="close-detail" onClick={onClose} aria-label="关闭详情">×</button></div></header><div className="detail-body">{card.detailSections.map((section, index) => <section className="detail-section" key={`${section.title}-${index}`}><h2>{section.title}</h2>{section.content && <p>{section.content}</p>}{section.items && <ol>{section.items.map((item) => <li key={item}>{item}</li>)}</ol>}</section>)}{qa.length > 0 && <section className="qa-section"><h2>对话记录</h2>{qa.map((item, index) => <div className="qa-pair" key={`${item.q}-${index}`}><div className="chat-message user"><div className="chat-bubble">{item.q}</div></div><div className="chat-message assistant"><div className="chat-bubble">{item.a}</div></div></div>)}</section>}<section className="ask-section"><h2>继续追问</h2><p>仅围绕当前{card.label}内容回答。</p><div className="ask-box"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：帮我把最关键的下一步说得更具体" /><button className="primary" onClick={ask}>发送</button></div></section></div>{feedbackOpen && <FeedbackModal rating={rating} comment={comment} onRating={setRating} onComment={setComment} onSubmit={submitFeedback} onClose={closeFeedback} />}</div>;
}

function History({ state }) {
  return <main className="page-container"><div className="page-heading"><h1>音频历史</h1><p>已完成分析的音频会自动保存在本机。</p></div>{state.history.length === 0 ? <div className="page-empty"><h2>还没有历史音频</h2><p>完成一次整批分析后，音频会出现在这里。</p></div> : state.history.map((batch) => <section className="history-day" key={batch.id}><div className="date-divider"><b>{batch.date}</b></div><div className="history-batch"><div className="history-batch-title"><b>{batch.uploadedAt} 上传</b><span>{batch.files.length} 个音频</span></div><div className="audio-list">{batch.files.map((file, index) => <div className="audio-row" key={`${file.name}-${index}`}><div className="audio-type">{file.type}</div><div><b>{file.name}</b><span>{file.size} · {file.duration}</span></div><div className="audio-time"><b>{file.time}</b><span>本地文件</span></div></div>)}</div></div></section>)}</main>;
}

function PromptSettings({ state, update, scene, setScene, draft, setDraft, editing, setEditing, onToast }) {
  const active = SCENES.find((item) => item.id === scene);
  function save() {
    try { update((next) => savePromptRevision(next, scene, draft)); setEditing(false); onToast('Prompt 已保存，新分析将使用该版本'); } catch (error) { onToast(error.message); }
  }
  return <main className="page-container prompt-page"><div className="page-heading"><h1>Prompt 设置</h1><p>修改后只影响之后新分析的音频，历史结果不会重新生成。</p></div><div className="prompt-workspace"><aside><h2>分析场景</h2>{SCENES.map((item) => <button key={item.id} className={scene === item.id ? 'active' : ''} onClick={() => setScene(item.id)}><span>{item.name}</span><small>v{state.prompts[item.id].version}</small></button>)}</aside><section className="prompt-editor"><div className="prompt-editor-head"><div><h2>{active.name}</h2><p>本地文件：prompts/{scene}/current.md</p></div><button className="secondary" disabled={editing} onClick={() => setEditing(true)}>{editing ? '编辑中' : '编辑'}</button></div><div className="prompt-info">可自由修改完整自然语言 Prompt。系统基础规则和输出 Schema 由程序维护，不会随此内容变化。</div><textarea className="prompt-textarea" readOnly={!editing} value={draft} onChange={(event) => setDraft(event.target.value)} /><div className="prompt-actions"><span>{editing ? '修改后点击保存才会生效' : '当前为只读状态'}</span><button className="primary" disabled={!editing || !draft.trim()} onClick={save}>保存</button></div></section></div></main>;
}

function ProviderModal({ state, update, onClose, onToast }) {
  const [providerId, setProviderId] = useState(state.activeProvider);
  const [key, setKey] = useState('');
  const [status, setStatus] = useState({ type: '', message: '' });
  const [checking, setChecking] = useState(false);
  async function submit() {
    setChecking(true); setStatus({ type: '', message: '' });
    const result = await validateProviderKey(providerId, key);
    setChecking(false); setStatus({ type: result.ok ? 'success' : 'error', message: result.message });
    if (result.ok) { update((next) => { next.activeProvider = providerId; next.providers[providerId].configured = true; next.providers[providerId].lastChecked = '刚刚'; }); onToast(`${state.providers[providerId].name} 已配置`); setTimeout(onClose, 450); }
  }
  return <div className="modal-backdrop"><section className="modal provider-modal"><button className="modal-close" onClick={onClose}>×</button><h1>配置分析模型</h1><p>选择预制厂商并填写 API Key，保存时会立即校验是否可用。</p><div className="provider-tabs">{Object.entries(state.providers).map(([id, provider]) => <button key={id} className={providerId === id ? 'active' : ''} onClick={() => { setProviderId(id); setStatus({ type: '', message: '' }); }}>{provider.name}{provider.configured && <small>已配置</small>}</button>)}</div><label>API Key<input type="password" value={key} onChange={(event) => setKey(event.target.value)} placeholder={`填写 ${state.providers[providerId].name} API Key`} autoFocus /></label>{status.message && <div className={`validation ${status.type}`}>{status.message}</div>}<div className="modal-actions"><button className="secondary" onClick={onClose}>取消</button><button className="primary" disabled={checking} onClick={submit}>{checking ? '正在校验…' : '保存并校验'}</button></div></section></div>;
}

function ClearModal({ onClose, onConfirm }) {
  return <div className="modal-backdrop"><section className="modal clear-modal"><div className="warning-mark">!</div><h1>清除所有历史数据？</h1><p>此操作会永久删除本地历史，无法恢复。删除完成后，首页将回到“先上传音频”。</p><div className="delete-list"><b>将被删除</b><ul><li>所有已上传的音频文件与转写全文</li><li>所有模型生成的卡片、详情和完整问答</li><li>全局待办与由音频建立的个人画像</li></ul></div><div className="keep-note">不会删除：模型厂商与 API Key 配置、Prompt 配置、已提交的意见反馈。</div><div className="modal-actions"><button className="secondary" onClick={onClose}>取消</button><button className="danger-solid" onClick={onConfirm}>永久清除</button></div></section></div>;
}
