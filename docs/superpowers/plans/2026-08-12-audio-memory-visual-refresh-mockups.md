# Audio Memory Visual Refresh Mockups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce three production-quality, browser-viewable visual directions for the existing Audio Memory homepage, feed, and analysis-detail pages without changing their structure or workflow.

**Architecture:** Each direction is a standalone HTML prototype with three screen states selected by a small preview-only toolbar. The prototypes reuse the current product's content hierarchy and controls while varying only tokens, typography, spacing, component treatment, iconography, and restrained motion. A lightweight Node verifier checks required screens, copy, accessibility hooks, and prohibited visual patterns before browser screenshots are accepted.

**Tech Stack:** Standalone HTML, CSS, minimal vanilla JavaScript, Node.js verification script, Codex in-app browser screenshots.

## Global Constraints

- Preserve the existing top navigation, left-side model/upload controls, feed timeline, result cards, detail content, evidence, and follow-up input.
- Homepage title must be `你的高级个人分析顾问`.
- Do not add product modules, navigation destinations, dashboards, chat personas, or a redesigned user journey.
- Do not use glowing AI orbs, neon colors, large gradients, glassmorphism, decorative sci-fi graphics, or magazine-style visual gimmicks.
- Body copy must be at least 14px; primary reading content must be 15–16px.
- Primary controls and mobile touch targets must be at least 44px high.
- Motion must communicate upload, analysis, completion, or content arrival; honor `prefers-reduced-motion`.
- Validate at desktop `1440×1024` and mobile `390×844`.

---

### Task 1: Build the mockup verification contract

**Files:**
- Create: `docs/working/audio-memory-visual-refresh-v2/verify.mjs`

**Interfaces:**
- Consumes: three option files named `warm-technology.html`, `clear-professional.html`, and `soft-companion.html` in the same directory.
- Produces: exit code `0` when every option contains the required screen states, copy, accessibility hooks, and responsive/motion rules; non-zero with a precise failure message otherwise.

- [ ] **Step 1: Write the verifier with required assertions**

```js
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const files = ['warm-technology.html', 'clear-professional.html', 'soft-companion.html'];
const required = [
  'data-screen="home"',
  'data-screen="feed"',
  'data-screen="detail"',
  '你的高级个人分析顾问',
  '模型与 API Key',
  '上传音频',
  '本次概览',
  '核心结论',
  '来自原声的依据',
  '继续追问',
  'prefers-reduced-motion',
  '@media (max-width: 720px)',
];

for (const file of files) {
  const source = fs.readFileSync(path.join(root, file), 'utf8');
  for (const token of required) {
    if (!source.includes(token)) throw new Error(`${file}: missing ${token}`);
  }
  if (/glowing orb|neon|glassmorphism/i.test(source)) {
    throw new Error(`${file}: prohibited visual direction`);
  }
}

console.log('3 visual directions verified');
```

- [ ] **Step 2: Run the verifier and confirm the expected initial failure**

Run: `node docs/working/audio-memory-visual-refresh-v2/verify.mjs`

Expected: FAIL because the three option files do not exist yet.

- [ ] **Step 3: Commit the verification contract**

```bash
git add docs/working/audio-memory-visual-refresh-v2/verify.mjs
git commit -m "test: define audio memory visual mockup contract"
```

### Task 2: Create the warm technology direction

**Files:**
- Create: `docs/working/audio-memory-visual-refresh-v2/warm-technology.html`

**Interfaces:**
- Consumes: the required tokens enforced by `verify.mjs` and existing product structure from `prototype/src/App.jsx`.
- Produces: a standalone visual prototype with `showScreen('home' | 'feed' | 'detail')` and a preview toolbar using `data-preview-target` buttons.

- [ ] **Step 1: Implement the three existing screen states**

Create semantic sections with `data-screen="home"`, `data-screen="feed"`, and `data-screen="detail"`. Preserve the existing topbar, left control rail, feed timeline, analysis cards, detail hierarchy, evidence playback, and follow-up input.

- [ ] **Step 2: Apply the warm technology visual system**

Use warm neutral surfaces, deep ink-blue type, desaturated periwinkle for selection and primary actions, and a restrained warm accent only for urgent actions. Use CSS variables, consistent 10–12px radii, 1px borders, minimal shadows, 15–16px reading text, and a quiet waveform treatment inside the existing upload zone.

- [ ] **Step 3: Add state switching and accessible motion**

Implement `showScreen(screen)` to update `[data-screen]`, `[data-preview-target]`, `aria-selected`, and the URL hash. Add 150–240ms content transitions and disable them inside `@media (prefers-reduced-motion: reduce)`.

- [ ] **Step 4: Run the verifier and inspect this direction in the browser**

Run: `node docs/working/audio-memory-visual-refresh-v2/verify.mjs`

Expected: FAIL only for the two option files not created yet; no failure mentioning `warm-technology.html`.

- [ ] **Step 5: Commit the direction**

```bash
git add docs/working/audio-memory-visual-refresh-v2/warm-technology.html
git commit -m "design: add warm technology audio memory mockup"
```

### Task 3: Create the clear professional direction

**Files:**
- Create: `docs/working/audio-memory-visual-refresh-v2/clear-professional.html`

**Interfaces:**
- Consumes: the same screen contract and current product hierarchy as Task 2.
- Produces: a standalone visual prototype with the same `showScreen('home' | 'feed' | 'detail')` interface.

- [ ] **Step 1: Implement the same three screen states and content density**

Match Task 2's visible product content and navigation structure so the user compares visual systems rather than different products.

- [ ] **Step 2: Apply the clear professional visual system**

Use cool white surfaces, graphite typography, dark navy primary actions, sharper 6–8px radii, strong alignment, compact metadata, and near-flat elevation. Express intelligence through precise status labels, evidence timestamps, and clean analysis hierarchy.

- [ ] **Step 3: Add the same switching, focus, responsive, and reduced-motion behavior**

Ensure preview controls have `aria-selected`; buttons and inputs have visible `:focus-visible`; mobile controls are at least 44px; use the same URL hash behavior.

- [ ] **Step 4: Run the verifier and inspect this direction in the browser**

Run: `node docs/working/audio-memory-visual-refresh-v2/verify.mjs`

Expected: FAIL only for `soft-companion.html`.

- [ ] **Step 5: Commit the direction**

```bash
git add docs/working/audio-memory-visual-refresh-v2/clear-professional.html
git commit -m "design: add clear professional audio memory mockup"
```

### Task 4: Create the soft companion direction

**Files:**
- Create: `docs/working/audio-memory-visual-refresh-v2/soft-companion.html`

**Interfaces:**
- Consumes: the same screen contract and product hierarchy as Tasks 2–3.
- Produces: a standalone visual prototype with the same `showScreen('home' | 'feed' | 'detail')` interface.

- [ ] **Step 1: Implement the same three screen states and content density**

Use identical content categories and actions to the other directions; do not introduce memory-library or assistant-chat features.

- [ ] **Step 2: Apply the soft companion visual system**

Use ivory surfaces, grey-blue type, muted sage for positive/status cues, soft blue for primary actions, 12px radii, generous but not sparse spacing, and steady low-contrast surfaces. Keep the overall look contemporary and software-like.

- [ ] **Step 3: Add the same switching, focus, responsive, and reduced-motion behavior**

Implement the shared state contract, visible keyboard focus, 44px mobile targets, and reduced-motion support.

- [ ] **Step 4: Run the full verifier**

Run: `node docs/working/audio-memory-visual-refresh-v2/verify.mjs`

Expected: PASS with `3 visual directions verified`.

- [ ] **Step 5: Commit the direction**

```bash
git add docs/working/audio-memory-visual-refresh-v2/soft-companion.html
git commit -m "design: add soft companion audio memory mockup"
```

### Task 5: Browser validation and handoff

**Files:**
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/warm-technology-home.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/warm-technology-feed.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/warm-technology-detail.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/clear-professional-home.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/clear-professional-feed.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/clear-professional-detail.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/soft-companion-home.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/soft-companion-feed.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/soft-companion-detail.png`
- Create: `docs/working/audio-memory-visual-refresh-v2/screenshots/mobile-home-check.png`

**Interfaces:**
- Consumes: the three standalone prototypes.
- Produces: accepted screenshot evidence for each core screen and a mobile reflow check.

- [ ] **Step 1: Start a local static server**

Run: `python3 -m http.server 8091 --bind 127.0.0.1 --directory docs/working/audio-memory-visual-refresh-v2`

Expected: local server listens on port 8091.

- [ ] **Step 2: Capture every desktop screen at 1440×1024**

Open each option, select `home`, `feed`, and `detail`, and save one viewport screenshot per state using the exact filenames above.

- [ ] **Step 3: Inspect all nine desktop screenshots**

Reject and fix any screenshot with cropped navigation, collapsed columns, body copy below 14px, an unclear primary action, excessive card nesting, or visual structure that diverges from the existing product.

- [ ] **Step 4: Capture and inspect a mobile homepage at 390×844**

Verify navigation reflow, 44px touch targets, readable body copy, no horizontal scrolling, and preserved upload-first hierarchy.

- [ ] **Step 5: Run final verification**

Run: `node docs/working/audio-memory-visual-refresh-v2/verify.mjs`

Expected: PASS with `3 visual directions verified`.

- [ ] **Step 6: Commit accepted screenshot evidence**

```bash
git add docs/working/audio-memory-visual-refresh-v2/screenshots
git commit -m "docs: capture audio memory visual directions"
```
