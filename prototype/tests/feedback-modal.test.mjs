import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

let server;
let FeedbackModal;

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    server: { middlewareMode: true },
    appType: 'custom',
    logLevel: 'silent',
  });
  ({ FeedbackModal } = await server.ssrLoadModule('/src/App.jsx'));
});

after(async () => {
  await server?.close();
});

function renderFeedbackModal(overrides = {}) {
  return renderToStaticMarkup(createElement(FeedbackModal, {
    rating: '',
    comment: '',
    onRating: () => {},
    onComment: () => {},
    onSubmit: () => {},
    onClose: () => {},
    ...overrides,
  }));
}

test('feedback modal keeps explanation controls hidden before an inaccurate rating', () => {
  assert.equal(typeof FeedbackModal, 'function');
  const markup = renderFeedbackModal();
  assert.match(markup, />意见反馈</);
  assert.match(markup, />完全准确</);
  assert.match(markup, />内容不准</);
  assert.doesNotMatch(markup, /<textarea/);
  assert.doesNotMatch(markup, />提交反馈</);
});

test('inaccurate feedback modal requires an explanation before submission', () => {
  assert.equal(typeof FeedbackModal, 'function');
  const emptyMarkup = renderFeedbackModal({ rating: 'inaccurate' });
  assert.match(emptyMarkup, /<textarea[^>]*required=""/);
  assert.match(emptyMarkup, /<button[^>]*disabled=""[^>]*>提交反馈/);

  const completedMarkup = renderFeedbackModal({
    rating: 'inaccurate',
    comment: '会议结论遗漏了预算限制',
  });
  assert.match(completedMarkup, /<textarea[^>]*>会议结论遗漏了预算限制<\/textarea>/);
  assert.doesNotMatch(completedMarkup, /<button[^>]*disabled=""[^>]*>提交反馈/);
});
