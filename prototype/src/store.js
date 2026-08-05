import { DEFAULT_PROMPTS, INITIAL_FEED, INITIAL_HISTORY, INITIAL_TODOS } from './defaults.js';

export const STORAGE_KEY = 'audio-memory-prototype-v1';
const CARD_ORDER = ['meeting', 'parenting', 'content', 'growth', 'inspiration'];

export function createInitialState({ seeded = false } = {}) {
  const prompts = Object.fromEntries(Object.entries(DEFAULT_PROMPTS).map(([id, current]) => [
    id,
    { current, version: 1, versions: [] },
  ]));
  return {
    version: 1,
    providers: {
      kimi: { name: 'Kimi', configured: seeded, lastChecked: seeded ? '今天 18:36' : '' },
      deepseek: { name: 'DeepSeek', configured: false, lastChecked: '' },
      openai: { name: 'OpenAI', configured: false, lastChecked: '' },
    },
    activeProvider: 'kimi',
    upload: { files: [], error: '', paused: false },
    job: null,
    feed: seeded ? structuredClone(INITIAL_FEED) : [],
    todos: seeded ? structuredClone(INITIAL_TODOS) : [],
    history: seeded ? structuredClone(INITIAL_HISTORY) : [],
    prompts,
    feedback: [],
    hiddenProfile: seeded ? { interests: ['AI 产品', '结构化表达'], updatedAt: Date.now() } : null,
  };
}

export function loadState(storage = globalThis.localStorage) {
  try {
    const parsed = JSON.parse(storage.getItem(STORAGE_KEY));
    if (parsed?.version !== 1) return createInitialState();
    return parsed;
  } catch {
    return createInitialState();
  }
}

export function saveState(storage = globalThis.localStorage, state) {
  storage.setItem(STORAGE_KEY, JSON.stringify(state));
}

export function clearHistoryLayers(state) {
  return {
    ...structuredClone(state),
    upload: { files: [], error: '', paused: false },
    job: null,
    feed: [],
    todos: [],
    history: [],
    hiddenProfile: null,
  };
}

export function savePromptRevision(state, sceneId, content) {
  const prompt = state.prompts[sceneId];
  if (!prompt || !content.trim()) throw new Error('Prompt 不能为空');
  prompt.versions.push({ version: prompt.version, content: prompt.current, savedAt: Date.now() });
  prompt.current = content;
  prompt.version += 1;
  return prompt;
}

export function orderCards(cards) {
  return [...cards].sort((a, b) => CARD_ORDER.indexOf(a.sceneId) - CARD_ORDER.indexOf(b.sceneId));
}

export function createFeedbackRecord(input) {
  return {
    id: `feedback-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    createdAt: new Date().toISOString(),
    sceneId: input.sceneId,
    audio: input.audio ?? [],
    transcript: input.transcript ?? '',
    generatedContent: input.generatedContent ?? null,
    promptVersion: input.promptVersion ?? 1,
    schemaVersion: '1.0',
    modelVersion: input.modelVersion ?? 'prototype-mock-1',
    rating: input.rating ?? '',
    comment: input.comment ?? '',
    qa: structuredClone(input.qa ?? []),
  };
}

export function getFeedbackFormState(rating, comment = '') {
  const showDetails = rating === 'inaccurate';
  return {
    showDetails,
    canSubmit: rating === 'accurate' || (showDetails && Boolean(comment.trim())),
  };
}

export function appendCardQA(batch, cardId, pair) {
  batch.qa ||= {};
  batch.qa[cardId] ||= [];
  batch.qa[cardId].push(structuredClone(pair));
  return structuredClone(batch.qa[cardId]);
}
