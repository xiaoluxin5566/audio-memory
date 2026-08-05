# Audio Memory Interactive Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-screen, locally runnable Audio Memory prototype whose product interactions match the approved PRD, while replacing real API validation, Whisper transcription, and model analysis with deterministic simulations.

**Architecture:** Use the bundled Product Design React/Vite prototype runtime. Keep product state in a versioned localStorage store, keep mock provider and analysis behavior in a separate deterministic engine, and render the three routes plus overlays from one application shell so navigation never opens a new browser tab.

**Tech Stack:** React 19, Vite 6, CSS3, JavaScript ES modules, Node.js 22 built-in test runner.

## Global Constraints

- Platform target is macOS Apple Silicon and local browser use.
- No login, registration, identity verification, or user roles.
- Top navigation is identical across Information Feed, Audio History, and Prompt Settings.
- Audio History opens in the same browser tab.
- Supported upload formats are MP3 and AAC only.
- Do not call real Kimi, DeepSeek, OpenAI, or Whisper services.
- The right feed never shows the current batch's partial upload, transcription, or analysis state.
- Prompt Settings exposes only six fixed scenes and only Edit and Save.
- The editable scene prompt never exposes or changes the system prompt, schema, or model parameters.
- Saving a scene prompt affects only subsequent analyses.
- Prototype UI must not include review-board titles, Prime Radiant, Connected, selection cards, or design annotations.
- All user-facing product copy is Simplified Chinese.

---

### Task 1: Local application shell and versioned state store

**Files:**
- Create: `prototype/package.json`
- Create: `prototype/server.mjs`
- Create: `prototype/index.html`
- Create: `prototype/src/data.js`
- Create: `prototype/src/store.js`
- Create: `prototype/tests/store.test.mjs`

**Interfaces:**
- Produces: `createInitialState(): AppState`, `loadState(storage): AppState`, `saveState(storage, state): void`, `clearHistoryLayers(state): AppState`.
- `AppState` contains `route`, `provider`, `upload`, `jobs`, `feed`, `todos`, `history`, `prompts`, `feedback`, and `ui`.

- [ ] **Step 1: Write state tests**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { createInitialState, clearHistoryLayers } from '../src/store.js';

test('initial state has six fixed prompt scenes', () => {
  assert.deepEqual(Object.keys(createInitialState().prompts), [
    'todo', 'meeting', 'parenting', 'content', 'growth', 'inspiration'
  ]);
});

test('clear preserves provider, prompts and feedback', () => {
  const state = createInitialState();
  state.provider.kimi.configured = true;
  state.feedback.push({ id: 'feedback-1' });
  const cleared = clearHistoryLayers(state);
  assert.equal(cleared.provider.kimi.configured, true);
  assert.equal(cleared.feedback.length, 1);
  assert.equal(cleared.feed.length, 0);
  assert.equal(cleared.history.length, 0);
});
```

- [ ] **Step 2: Run tests and confirm they fail**

Run: `cd prototype && node --test tests/store.test.mjs`

Expected: FAIL because `src/store.js` does not exist.

- [ ] **Step 3: Implement the HTTP server, initial data, migrations, load/save and clear functions**

The server must serve `/`, `/history`, and `/settings/prompts` from `index.html`, reject traversal outside `prototype/`, and serve correct MIME types. The state key is `audio-memory-prototype-v1`; invalid or older data falls back to `createInitialState()`.

- [ ] **Step 4: Run store tests**

Run: `cd prototype && node --test tests/store.test.mjs`

Expected: PASS.

### Task 2: Full-screen shell, unified navigation and provider configuration

**Files:**
- Create: `prototype/styles.css`
- Create: `prototype/src/app.js`
- Modify: `prototype/index.html`
- Create: `prototype/tests/provider.test.mjs`
- Create: `prototype/src/mock-engine.js`

**Interfaces:**
- Consumes: `loadState`, `saveState` from Task 1.
- Produces: `validateProviderKey(providerId, key): Promise<{ok:boolean,message:string}>` and route rendering for `/`, `/history`, `/settings/prompts`.

- [ ] **Step 1: Write provider simulation tests**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { validateProviderKey } from '../src/mock-engine.js';

test('empty and invalid keys fail validation', async () => {
  assert.equal((await validateProviderKey('kimi', '')).ok, false);
  assert.equal((await validateProviderKey('openai', 'invalid-demo-key')).ok, false);
});

test('non-empty demo key validates', async () => {
  assert.equal((await validateProviderKey('deepseek', 'demo-valid-key')).ok, true);
});
```

- [ ] **Step 2: Run provider tests and confirm failure**

Run: `cd prototype && node --test tests/provider.test.mjs`

Expected: FAIL because `validateProviderKey` is missing.

- [ ] **Step 3: Build the app shell and provider modal**

Implement one sticky top bar with `信息流`, `音频历史`, `Prompt 设置`, and `清除所有历史`. Use `history.pushState` and `popstate` so all navigation stays in one tab. Provider configuration must support Kimi, DeepSeek and OpenAI, preserve entered text on validation failure, show success/failure inline, and provide Modify and Revalidate after success.

- [ ] **Step 4: Implement deterministic provider validation and run tests**

Run: `cd prototype && node --test tests/provider.test.mjs`

Expected: PASS.

### Task 3: Upload queue, format handling and simulated analysis pipeline

**Files:**
- Modify: `prototype/src/mock-engine.js`
- Modify: `prototype/src/app.js`
- Modify: `prototype/styles.css`
- Create: `prototype/tests/analysis.test.mjs`
- Create: `prototype/fixtures/unsupported.txt`

**Interfaces:**
- Produces: `acceptAudioFile(fileLike): {ok:boolean,error?:string}`, `buildMockBatch(files, providerId, prompts): AnalysisBatch`, and job stages `uploading | transcribing | analyzing | failed | interrupted | completed`.

- [ ] **Step 1: Write file and result tests**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { acceptAudioFile, buildMockBatch } from '../src/mock-engine.js';

test('only mp3 and aac extensions are accepted', () => {
  assert.equal(acceptAudioFile({ name: 'a.mp3' }).ok, true);
  assert.equal(acceptAudioFile({ name: 'b.aac' }).ok, true);
  assert.equal(acceptAudioFile({ name: 'c.wav' }).ok, false);
});

test('mock batch contains the five ordered card scenes', () => {
  const result = buildMockBatch([{ name: 'demo.mp3' }], 'kimi', {});
  assert.deepEqual(result.cards.map(card => card.sceneId), [
    'meeting', 'parenting', 'content', 'growth', 'inspiration'
  ]);
});
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd prototype && node --test tests/analysis.test.mjs`

Expected: FAIL because the functions are missing.

- [ ] **Step 3: Implement drag/drop, file picker, per-file progress and unsupported-format pause**

The exact unsupported-format message is `不支持该文件格式，请上传 MP3 / AAC 格式文件`. Removing the bad file resumes the queue. Each accepted file displays name, format, size and upload progress.

- [ ] **Step 4: Implement simulated transcription, analysis, retry and interruption recovery**

Keep all progress on the left. The right feed remains unchanged until the whole batch completes. Persist an active job marker; after reload render a recovery panel with Continue and Cancel. A failure panel identifies the failed stage and offers Retry without replaying completed stages.

- [ ] **Step 5: Run analysis tests**

Run: `cd prototype && node --test tests/analysis.test.mjs`

Expected: PASS.

### Task 4: Feed, global todos, card details, chat and feedback

**Files:**
- Modify: `prototype/src/data.js`
- Modify: `prototype/src/app.js`
- Modify: `prototype/styles.css`
- Create: `prototype/tests/content.test.mjs`

**Interfaces:**
- Consumes: `AnalysisBatch` from Task 3.
- Produces: ordered batch feed, detail rendering from `detailSections`, todo operations, scoped QA records, and feedback records.

- [ ] **Step 1: Write content ordering and feedback tests**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { orderCards, createFeedbackRecord } from '../src/store.js';

test('cards use the approved batch order', () => {
  const input = ['inspiration', 'meeting', 'growth', 'parenting', 'content'];
  assert.deepEqual(orderCards(input.map(sceneId => ({ sceneId }))).map(x => x.sceneId), [
    'meeting', 'parenting', 'content', 'growth', 'inspiration'
  ]);
});

test('feedback captures scene, transcript and complete QA', () => {
  const result = createFeedbackRecord({
    sceneId: 'meeting', transcript: 'full transcript', qa: [{ q: 'Q', a: 'A' }]
  });
  assert.equal(result.sceneId, 'meeting');
  assert.equal(result.transcript, 'full transcript');
  assert.equal(result.qa.length, 1);
});
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd prototype && node --test tests/content.test.mjs`

Expected: FAIL because content helpers are missing.

- [ ] **Step 3: Implement the feed and global todo operations**

Global todos stay above the timeline. Support inline Edit, Complete and Delete. Overdue items are red and remain incomplete until manually checked; completed items move to a visually weakened bottom section.

- [ ] **Step 4: Implement covering details for all five cards**

Use a shared detail shell and render each scene from `detailSections`. Preserve scroll position when closing. Meeting detail must exclude transcription and expression advice. Family, content, growth and inspiration use the approved module names and realistic content.

- [ ] **Step 5: Implement scoped chat and expanded feedback**

Chat answers append below the card content after a divider and remain scoped to the selected card. Feedback shows `完全准确`, `内容不准`, and a free-text field directly in the detail page. Save a record with scene, batch/audio metadata, full transcript, full generated content, prompt/schema/model versions, user feedback and complete QA.

- [ ] **Step 6: Run content tests**

Run: `cd prototype && node --test tests/content.test.mjs`

Expected: PASS.

### Task 5: Audio History, Prompt Settings and clear-history behavior

**Files:**
- Modify: `prototype/src/app.js`
- Modify: `prototype/src/store.js`
- Modify: `prototype/styles.css`
- Create: `prototype/tests/prompts.test.mjs`

**Interfaces:**
- Produces: read-only chronological audio history, six-scene prompt editing, saved prompt versions, and clear confirmation behavior.

- [ ] **Step 1: Write prompt persistence tests**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { savePromptRevision, createInitialState } from '../src/store.js';

test('saving prompt archives previous content and increments version', () => {
  const state = createInitialState();
  const previous = state.prompts.meeting.current;
  savePromptRevision(state, 'meeting', 'new prompt');
  assert.equal(state.prompts.meeting.current, 'new prompt');
  assert.equal(state.prompts.meeting.versions.at(-1).content, previous);
  assert.equal(state.prompts.meeting.version, 2);
});
```

- [ ] **Step 2: Run test and confirm failure**

Run: `cd prototype && node --test tests/prompts.test.mjs`

Expected: FAIL because `savePromptRevision` is missing.

- [ ] **Step 3: Implement Audio History**

Show only completed batches, split by natural day and ordered newest first. Do not add status, details, edit, delete, search, playback or reanalysis controls. Provide empty and list states.

- [ ] **Step 4: Implement Prompt Settings**

Show only the six fixed scenes. Default state is read-only. Edit unlocks one complete natural-language editor; Save persists a revision and displays `Prompt 已保存，新分析将使用该版本`. No other prompt actions appear.

- [ ] **Step 5: Implement clear-history confirmation**

The modal lists deleted and preserved layers, has Cancel and Permanent Clear, and does not require typed confirmation. Clearing resets feed, audio, transcript, QA, todos and hidden profile while preserving provider configuration, prompts and feedback.

- [ ] **Step 6: Run prompt and store tests**

Run: `cd prototype && node --test tests/prompts.test.mjs tests/store.test.mjs`

Expected: PASS.

### Task 6: Full verification, screenshots and PRD handoff

**Files:**
- Create: `prototype/README.md`
- Modify: `Audio Memory 第一阶段产品PRD-v0.9.md`
- Create: `screenshots/product-home-fullscreen.png`
- Create: `screenshots/product-history-fullscreen.png`
- Create: `screenshots/product-prompts-fullscreen.png`
- Create: `screenshots/product-states-fullscreen.png`

**Interfaces:**
- Consumes: completed prototype and all test suites.
- Produces: terminal start instructions, verified prototype URL, full-screen implementation screenshots, and corrected PRD links.

- [ ] **Step 1: Run the complete automated suite**

Run: `cd prototype && node --test tests/*.test.mjs`

Expected: all tests pass with exit code 0.

- [ ] **Step 2: Start the local server and exercise the complete browser flow**

Run: `cd prototype && npm start`

Verify at 1440×900: provider failure/success, MP3/AAC upload, unsupported-format pause, transcription/analysis, recovery, completed result, all card details, chat, feedback, todo operations, history, prompt edit/save, and clear confirmation.

- [ ] **Step 3: Capture full-screen product screenshots**

Capture only the application viewport. Do not include browser chrome, visual companion chrome, review annotations or explanatory boards.

- [ ] **Step 4: Update PRD design links and state mapping**

Replace implementation-facing links to old design boards with the new prototype and full-screen screenshots. Keep old review boards only under an explicitly labeled archive section if preservation is useful.

- [ ] **Step 5: Sync the PRD, prototype and screenshots to the Desktop backup**

Copy to `/Users/liujinxin/Desktop/音频Always on/` and compare the PRD and prototype file counts with the workspace copies.
