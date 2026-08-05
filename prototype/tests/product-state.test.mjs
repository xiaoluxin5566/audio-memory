import test from 'node:test';
import assert from 'node:assert/strict';
import {
  appendCardQA,
  clearHistoryLayers,
  createFeedbackRecord,
  createInitialState,
  getFeedbackFormState,
  orderCards,
  savePromptRevision,
} from '../src/store.js';

test('initial state has six fixed prompt scenes', () => {
  assert.deepEqual(Object.keys(createInitialState().prompts), [
    'todo', 'meeting', 'parenting', 'content', 'growth', 'inspiration',
  ]);
});

test('clear preserves provider configuration, prompts and feedback', () => {
  const state = createInitialState();
  state.providers.kimi.configured = true;
  state.feedback.push({ id: 'feedback-1' });
  state.feed.push({ id: 'batch-1' });
  state.history.push({ id: 'history-1' });
  const cleared = clearHistoryLayers(state);
  assert.equal(cleared.providers.kimi.configured, true);
  assert.equal(cleared.feedback.length, 1);
  assert.equal(cleared.feed.length, 0);
  assert.equal(cleared.history.length, 0);
  assert.equal(cleared.todos.length, 0);
});

test('saving prompt archives previous content and increments version', () => {
  const state = createInitialState();
  const previous = state.prompts.meeting.current;
  savePromptRevision(state, 'meeting', 'new prompt');
  assert.equal(state.prompts.meeting.current, 'new prompt');
  assert.equal(state.prompts.meeting.versions.at(-1).content, previous);
  assert.equal(state.prompts.meeting.version, 2);
});

test('cards follow approved batch order', () => {
  const cards = ['inspiration', 'meeting', 'growth', 'parenting', 'content']
    .map((sceneId) => ({ sceneId }));
  assert.deepEqual(orderCards(cards).map((item) => item.sceneId), [
    'meeting', 'parenting', 'content', 'growth', 'inspiration',
  ]);
});

test('feedback records scene, transcript and complete QA', () => {
  const record = createFeedbackRecord({
    sceneId: 'meeting',
    transcript: 'full transcript',
    qa: [{ q: 'Q', a: 'A' }],
  });
  assert.equal(record.sceneId, 'meeting');
  assert.equal(record.transcript, 'full transcript');
  assert.equal(record.qa.length, 1);
  assert.ok(record.id);
});

test('feedback details are required only for inaccurate ratings', () => {
  assert.deepEqual(getFeedbackFormState(''), {
    showDetails: false,
    canSubmit: false,
  });
  assert.deepEqual(getFeedbackFormState('accurate'), {
    showDetails: false,
    canSubmit: true,
  });
  assert.deepEqual(getFeedbackFormState('inaccurate', '   '), {
    showDetails: true,
    canSubmit: false,
  });
  assert.deepEqual(getFeedbackFormState('inaccurate', '会议结论遗漏了预算限制'), {
    showDetails: true,
    canSubmit: true,
  });
});

test('appending card QA returns the updated conversation', () => {
  const batch = { qa: {} };
  const updated = appendCardQA(batch, 'card-1', { q: '问题', a: '回答' });
  assert.deepEqual(updated, [{ q: '问题', a: '回答' }]);
  assert.deepEqual(batch.qa['card-1'], updated);
});
