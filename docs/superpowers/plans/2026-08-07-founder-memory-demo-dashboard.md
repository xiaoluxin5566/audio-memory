# 创始人工作记忆演示看板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建一个可离线打开、只使用内置虚拟数据的创始人工作记忆成果看板，用于向老板演示产品核心价值。

**Architecture:** 在仓库根目录新增独立的静态网页目录 `demo-dashboard/`，不引用 `prototype/src`、后端或任何运行时接口。页面以 ES 模块中的不可变虚拟数据为唯一数据源，以浏览器内存管理筛选、搜索、日期和详情抽屉状态；刷新后回到初始数据。

**Tech Stack:** 原生 HTML、CSS、ES modules；使用现有 Vite 二进制启动本地预览，不新增依赖。

## Global Constraints

- 演示目录固定为 `demo-dashboard/`，不得修改 `prototype/src/`、`backend/`、真实数据目录或业务 API。
- 禁止 `fetch`、上传控件、API Key、模型调用、真实音频文件与网络请求。
- 所有示例人物、会议、决策和待办均为虚拟数据；页面常驻“演示数据”标识。
- 不展示完整逐字稿，只允许在详情中显示虚构的来源摘要与时间点。
- 刷新页面必须恢复预置数据；筛选和查看详情不得写入 localStorage、Cookie 或后端。

---

### Task 1: 建立隔离的虚拟数据与应用骨架

**Files:**
- Create: `demo-dashboard/index.html`
- Create: `demo-dashboard/demo-data.js`
- Create: `demo-dashboard/app.js`
- Create: `demo-dashboard/styles.css`
- Test: `demo-dashboard/demo-data.test.mjs`

**Interfaces:**
- Produces: `DEMO_DATA` from `demo-data.js`, containing `summary`, `highlights`, `memories`, `tasks`, `people`, `recordings`, and `connections` arrays.
- Produces: `getDashboardState()` and `renderDashboard(root, state)` from `app.js` for the later interaction task.

- [ ] **Step 1: Write the failing virtual-data test**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { DEMO_DATA } from './demo-data.js';

test('demo data covers the executive dashboard narrative', () => {
  assert.equal(DEMO_DATA.isDemo, true);
  assert.ok(DEMO_DATA.memories.length >= 6);
  assert.ok(DEMO_DATA.tasks.length >= 4);
  assert.ok(DEMO_DATA.people.length >= 3);
  assert.ok(DEMO_DATA.recordings.length >= 3);
  assert.ok(DEMO_DATA.connections.length >= 2);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test demo-dashboard/demo-data.test.mjs`

Expected: FAIL because `demo-data.js` does not exist.

- [ ] **Step 3: Implement the static data contract and page shell**

```js
export const DEMO_DATA = Object.freeze({
  isDemo: true,
  summary: { listeningHours: 8.6, memories: 24, openTasks: 7, keyPeople: 6 },
  highlights: [], memories: [], tasks: [], people: [], recordings: [], connections: [],
});
```

Create `index.html` with only `<main id="app"></main>` and a module script for `app.js`. In `app.js`, export `getDashboardState()` returning `{ filter: 'all', query: '', dateRange: 'week', detail: null }`; call `renderDashboard(document.querySelector('#app'), getDashboardState())`. Render a text-only initial dashboard shell from `DEMO_DATA`.

- [ ] **Step 4: Run the data test to verify it passes**

Run: `node --test demo-dashboard/demo-data.test.mjs`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add demo-dashboard/index.html demo-dashboard/demo-data.js demo-dashboard/app.js demo-dashboard/styles.css demo-dashboard/demo-data.test.mjs
git commit -m "feat: add isolated demo dashboard data"
```

### Task 2: 实现高管晨报式总览与可点击成果区

**Files:**
- Modify: `demo-dashboard/app.js`
- Modify: `demo-dashboard/styles.css`
- Test: `demo-dashboard/dashboard-render.test.mjs`

**Interfaces:**
- Consumes: `DEMO_DATA`, `getDashboardState()` and `renderDashboard(root, state)` from Task 1.
- Produces: `buildDashboardMarkup(state)` returning accessible HTML for the summary, highlights, memories, task list, people list, recordings and connections.

- [ ] **Step 1: Write the failing rendering test**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildDashboardMarkup, getDashboardState } from './app.js';

test('dashboard renders demo label and core result sections', () => {
  const markup = buildDashboardMarkup(getDashboardState());
  for (const label of ['演示数据', '今日重点', '工作记忆', '待办追踪', '人物洞察', '跨录音关联']) {
    assert.match(markup, new RegExp(label));
  }
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test demo-dashboard/dashboard-render.test.mjs`

Expected: FAIL because `buildDashboardMarkup` is not exported.

- [ ] **Step 3: Implement the complete visual layout**

Implement `buildDashboardMarkup(state)` using `DEMO_DATA`. Include:

```html
<header class="masthead"><span class="demo-badge">演示数据</span></header>
<section aria-label="本周概览" class="metric-grid">...</section>
<section aria-labelledby="focus-title" class="highlights">...</section>
<section aria-labelledby="memory-title" class="memory-section">...</section>
```

Add buttons with `data-action="open-detail"`, `data-kind` and `data-id` to memory, task, person, recording and connection entries. Style with an editorial, executive-brief aesthetic: warm paper background, dark ink, restrained red/orange decision accent, dense but readable cards, responsive grid, keyboard-visible focus rings. Do not reuse or import existing product CSS.

- [ ] **Step 4: Run the rendering test and build a local preview**

Run: `node --test demo-dashboard/dashboard-render.test.mjs`

Expected: PASS.

Run: `prototype/node_modules/.bin/vite --host 127.0.0.1 --port 4174 demo-dashboard`

Expected: local page responds and contains no network-dependent content.

- [ ] **Step 5: Commit**

```bash
git add demo-dashboard/app.js demo-dashboard/styles.css demo-dashboard/dashboard-render.test.mjs
git commit -m "feat: build executive demo dashboard"
```

### Task 3: 加入可交互筛选、搜索与详情抽屉

**Files:**
- Modify: `demo-dashboard/app.js`
- Modify: `demo-dashboard/styles.css`
- Create: `demo-dashboard/interactions.test.mjs`

**Interfaces:**
- Consumes: `DEMO_DATA`, `getDashboardState()`, `buildDashboardMarkup(state)`, `renderDashboard(root, state)`.
- Produces: `reduceDashboardState(state, action)` for deterministic filter/search/date/detail transitions and `getDetailRecord(kind, id)` for drawer lookup.

- [ ] **Step 1: Write failing pure-state tests**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { getDashboardState, reduceDashboardState, getDetailRecord } from './app.js';

test('dashboard state changes stay in browser memory', () => {
  const start = getDashboardState();
  const filtered = reduceDashboardState(start, { type: 'filter', filter: '客户反馈' });
  assert.equal(filtered.filter, '客户反馈');
  const searched = reduceDashboardState(filtered, { type: 'search', query: '定价' });
  assert.equal(searched.query, '定价');
});

test('a known record opens with source-only summary', () => {
  const item = getDetailRecord('memory', 'mem-pricing');
  assert.equal(item.id, 'mem-pricing');
  assert.ok(item.sourceSummary);
  assert.equal(item.transcript, undefined);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test demo-dashboard/interactions.test.mjs`

Expected: FAIL because the state reducer and record lookup are not exported.

- [ ] **Step 3: Implement browser-only interactions**

Implement pure state transitions for `filter`, `search`, `dateRange`, `openDetail`, and `closeDetail`. Attach a single click handler and input handler in `renderDashboard`; each state update re-renders the same root without persistence. The drawer must show title, conclusion, source recording name, time point, a limited source summary, related people/tasks, and a close button. Handle Escape to close and return focus to the originating trigger where available.

- [ ] **Step 4: Run interaction and rendering tests**

Run: `node --test demo-dashboard/demo-data.test.mjs demo-dashboard/dashboard-render.test.mjs demo-dashboard/interactions.test.mjs`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add demo-dashboard/app.js demo-dashboard/styles.css demo-dashboard/interactions.test.mjs
git commit -m "feat: add demo dashboard interactions"
```

### Task 4: 完成隔离与浏览器验收

**Files:**
- Modify: `demo-dashboard/app.js`
- Modify: `demo-dashboard/styles.css`
- Create: `demo-dashboard/README.md`
- Test: `demo-dashboard/isolation.test.mjs`

**Interfaces:**
- Consumes: all Task 1–3 exports.
- Produces: a documented, standalone demo that can be opened locally without product services.

- [ ] **Step 1: Write the failing isolation test**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('demo dashboard has no production or network dependency', async () => {
  const sources = await Promise.all(['index.html', 'app.js', 'demo-data.js'].map((file) => readFile(new URL(file, import.meta.url), 'utf8')));
  const source = sources.join('\n');
  assert.doesNotMatch(source, /fetch\s*\(|XMLHttpRequest|localStorage|api\/|upload/i);
  assert.match(source, /演示数据/);
});
```

- [ ] **Step 2: Run test to verify it fails or exposes an accidental dependency**

Run: `node --test demo-dashboard/isolation.test.mjs`

Expected: PASS only after the implementation has no prohibited dependency.

- [ ] **Step 3: Finish accessibility and usage documentation**

Add responsive behavior for narrow screens, focus styles, semantic headings, visible close controls, and `aria-label`s. Write `README.md` stating that this is a static virtual-data demo, has no access to production data, and can be previewed with:

```bash
prototype/node_modules/.bin/vite --host 127.0.0.1 --port 4174 demo-dashboard
```

- [ ] **Step 4: Run all standalone checks and manually verify in browser**

Run: `node --test demo-dashboard/*.test.mjs`

Expected: PASS.

Run: `prototype/node_modules/.bin/vite --host 127.0.0.1 --port 4174 demo-dashboard`

Expected: page loads with all core sections; filtering, searching and details work; browser network panel has no application API calls.

- [ ] **Step 5: Commit**

```bash
git add demo-dashboard/app.js demo-dashboard/styles.css demo-dashboard/README.md demo-dashboard/isolation.test.mjs
git commit -m "docs: document isolated demo dashboard"
```

## Plan self-review

- Spec coverage: Tasks 1–2 implement the isolated virtual data and executive result dashboard; Task 3 implements the approved clickable drill-down, filtering, search and date switching; Task 4 validates no API/data impact and offline behavior.
- Placeholder scan: no TBD/TODO or unspecified implementation/testing steps remain.
- Type consistency: Tasks 2–4 consume the named Task 1/3 exports; record kinds are limited to `memory`, `task`, `person`, `recording`, and `connection`.
