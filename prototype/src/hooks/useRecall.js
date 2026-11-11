import { useCallback, useEffect, useRef, useState } from 'react';
import { recallApi } from '../api/recall.js';
const KEY = 'audio-memory-recall-session';
export function useRecall() {
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(KEY));
  const [turns, setTurns] = useState([]);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef(null);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    const next = await recallApi.status();
    if (mounted.current) setStatus(next);
    if (sessionId) {
      try { const data = await recallApi.session(sessionId); if (mounted.current) setTurns(data.turns); }
      catch (failure) { if (failure.status === 404) { localStorage.removeItem(KEY); setSessionId(null); setTurns([]); } else throw failure; }
    }
  }, [sessionId]);
  useEffect(() => { mounted.current = true; refresh().catch((failure) => setError(failure.message)); return () => { mounted.current = false; }; }, [refresh]);
  useEffect(() => {
    if (!sessionId || !turns.some((turn) => !['completed', 'failed'].includes(turn.status))) return undefined;
    let stopped = false; let timer;
    const poll = async () => {
      try { const data = await recallApi.session(sessionId); if (!stopped) { setTurns(data.turns); setError(''); if (data.turns.some((turn) => !['completed', 'failed'].includes(turn.status))) timer = setTimeout(poll, 1200); } }
      catch (failure) { if (!stopped) { if (failure.status === 404) { localStorage.removeItem(KEY); setSessionId(null); setTurns([]); pending.current = null; } else { setError(failure.message); timer = setTimeout(poll, 1200); } } }
    };
    timer = setTimeout(poll, 1200);
    return () => { stopped = true; clearTimeout(timer); };
  }, [sessionId, turns.some((turn) => !['completed', 'failed'].includes(turn.status))]);
  const submit = async (question, retry = false) => {
    setBusy(true); setError('');
    if (!retry || !pending.current) pending.current = { question, id: crypto.randomUUID() };
    try {
      let identity = sessionId;
      if (!identity) { identity = (await recallApi.createSession()).session_id; localStorage.setItem(KEY, identity); if (mounted.current) setSessionId(identity); }
      await recallApi.submit(identity, pending.current.question, pending.current.id);
      const data = await recallApi.session(identity); if (mounted.current) setTurns(data.turns);
      pending.current = null;
      return true;
    } catch (failure) { if (mounted.current) setError(failure.message); return false; }
    finally { if (mounted.current) setBusy(false); }
  };
  const retryTurn = async (id) => {
    setBusy(true); setError('');
    try { await recallApi.retryTurn(id); await refresh(); }
    catch (failure) { if (mounted.current) setError(failure.message); }
    finally { if (mounted.current) setBusy(false); }
  };
  return { retryTurn, turns, status, error, busy, submit, retry: () => submit('', true), canRetry: Boolean(pending.current), refresh };
}
