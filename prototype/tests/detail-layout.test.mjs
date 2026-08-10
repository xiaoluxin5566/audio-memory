import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const appSource = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');

test('detail page keeps the question composer after conversation history', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  const conversation = detail.indexOf('className="qa-section"');
  const composer = detail.indexOf('className="ask-section"');

  assert.ok(conversation > -1 && composer > -1);
  assert.ok(conversation < composer, 'question composer should be the final detail module');
});

test('conversation renders user and assistant messages as chat bubbles', () => {
  assert.match(appSource, /className="chat-message user"/);
  assert.match(appSource, /className="chat-message assistant"/);
  assert.match(styles, /\.chat-message\.user\{[^}]*justify-content:flex-end/);
  assert.match(styles, /\.chat-message\.assistant\{[^}]*justify-content:flex-start/);
});

test('feed and detail copy use readable full-screen type sizes', () => {
  assert.match(styles, /\.result-card h3\{font-size:16px/);
  assert.match(styles, /\.result-card p\{font-size:13px/);
  assert.match(styles, /\.detail-section p\{font-size:14px/);
  assert.match(styles, /\.detail-section li\{[^}]*font-size:13px/);
});

test('detail renders common blocks rather than strict scene payload keys', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.match(detail, /card\.detailSections\.map/)
  assert.doesNotMatch(detail, /inferred_title_hint|evidence_segment_ids|generation_reason|finding_id|case_id/)
});

test('detail exposes bounded evidence playback without transcript text', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.match(detail, /<EvidencePlayback evidence=\{card\.evidence\}/)
  assert.match(appSource, /<summary>回听证据 · \{evidence\.length\} 段<\/summary>/)
  assert.match(appSource, /<audio key=\{source\} controls preload="metadata" src=\{source\}/)
  assert.match(appSource, /#t=\$\{\(active\.startMs \/ 1000\)\.toFixed\(3\)\},/)
  assert.doesNotMatch(appSource, /active\.text|evidence\.text/)
  assert.match(styles, /\.evidence-playback\{/)
});
