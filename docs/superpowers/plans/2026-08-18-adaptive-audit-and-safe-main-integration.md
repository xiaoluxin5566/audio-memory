# 自适应审计与当日改进安全合并 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现按模型能力动态分块、可仅恢复失败审计块的报告主链路，并将防休眠、平滑进度、5 分钟 Whisper 块级恢复与当日 UI 修复验证后合入 `main`。

**Architecture:** 在独立集成分支上先合入已审查的报告主链路，再以独立策略模块计算每个 provider/model 的审计 Token 预算。审计块和 Whisper 物理块都使用指纹化、原子写入的内部检查点；只在全量覆盖校验成功后发布完整结果。其他当日改进以独立提交逐层合入，每层都有定向测试。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Pydantic, pytest, React, Vite, Node test runner, FFmpeg, MLX Whisper

**Spec:** `docs/superpowers/specs/2026-08-18-adaptive-audit-and-safe-main-integration-design.md`

## Global Constraints

- 只在 `.worktrees/main-preview` 的 `codex/adaptive-audit-main-integration` 分支工作；原运行目录保持不动。
- 不读取 Keychain，不调用真实付费 provider，不复制本地数据库和输出产物。
- 每个生产行为先写会因缺少该行为而失败的测试，确认 RED 后才实现 GREEN。
- 审计结果覆盖不完整时不得把任务标记为完整完成。
- 五分钟 Whisper 内部检查点不提前写入正式 `transcripts` 表。
- 旧检查点和旧任务必须保持向后兼容。
- 不执行 `git reset --hard`、`git clean`、整树 checkout 或 `git add .`。

---

### Task 1: 建立可信基线与当日改进清单

**Files:**
- Create: `docs/working/2026-08-18-adaptive-audit-integration-baseline.md`
- Read: Git branches `main`, `codex/report-audit-revision-pipeline`, `codex/smooth-progress`, `codex/analysis-sleep-prevention`

**Interfaces:**
- Consumes: `main` at `14d715a`, approved spec, unique branch-tip commits.
- Produces: exact commit/file inventory and clean baseline test evidence.

- [ ] **Step 1: Record branch and worktree state**

Run `git status --short --branch`, `git worktree list --porcelain`, and `git log --all --since='2026-08-18 00:00:00 +0800' --oneline --decorate`.

- [ ] **Step 2: Run clean baseline tests**

Run:

```bash
cd backend
PYTHONPATH=src ./.venv/bin/pytest -q tests/unit tests/integration/test_single_report_runner.py tests/integration/test_upload_jobs.py
cd ../prototype
npm test
npm run build
```

If `.venv` or `node_modules` is absent in the worktree, use the existing dependency directories by explicit absolute path or symlink only inside this worktree; do not install or modify global dependencies.

- [ ] **Step 3: Write the baseline inventory**

Record exact pass/fail counts, unique commits, files to include, files to exclude, and any baseline failures in `docs/working/2026-08-18-adaptive-audit-integration-baseline.md`.

- [ ] **Step 4: Commit the baseline**

Stage only the baseline file and commit with `docs: record adaptive audit integration baseline`.

---

### Task 2: 合入报告审计与定向修订主链路

**Files:**
- Merge commits: `5cdd954`, `9b0cc91`, `dfe9f8f`, `a61234b`
- Verify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Verify: `backend/src/audio_memory/analysis/segmented_report_audit.py`
- Verify: `backend/src/audio_memory/analysis/direct_report_pipeline.py`
- Verify: `backend/tests/integration/test_single_report_runner.py`

**Interfaces:**
- Consumes: generated V1 Markdown report and reliable transcript segments.
- Produces: chunk V1 audits, merged audit, one targeted V2 revision, final audit, published quality metadata.

- [ ] **Step 1: Cherry-pick the four report commits in dependency order**

Run one cherry-pick per commit. Resolve conflicts by preserving later `main` behavior and the report commit's audited-report behavior; never take an entire file blindly when both sides changed.

- [ ] **Step 2: Run report tests**

Run:

```bash
cd backend
PYTHONPATH=src ./.venv/bin/pytest -q \
  tests/unit/analysis/test_segmented_report_audit.py \
  tests/unit/analysis/test_direct_report_pipeline.py \
  tests/unit/prompts/test_direct_report_prompt.py \
  tests/integration/test_single_report_runner.py
```

- [ ] **Step 3: Inspect the resulting diff**

Confirm no provider model-selection rollback, deleted GLM adapter, local outputs, databases, or unrelated historical experiments entered the branch.

---

### Task 3: 新增模型能力审计策略

**Files:**
- Create: `backend/src/audio_memory/analysis/audit_model_policy.py`
- Modify: `backend/src/audio_memory/analysis/segmented_report_audit.py`
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Create: `backend/tests/unit/analysis/test_audit_model_policy.py`
- Modify: `backend/tests/unit/analysis/test_segmented_report_audit.py`

**Interfaces:**
- Produces: `AuditModelPolicy`, `resolve_audit_model_policy(provider_id: str, model_id: str) -> AuditModelPolicy`, and `audit_transcript_budget_chars(policy, fixed_prompt_chars: int) -> int`.
- Consumes: provider ID, model ID, fixed Prompt/report/schema size, transcript segments.

- [ ] **Step 1: Write failing policy-selection tests**

Add literal expectations proving GLM 5.2, DeepSeek, Kimi long-context, and an unknown model select distinct safe budgets; the unknown model must receive the most conservative supported policy. The tests must fail because the module/functions do not exist.

- [ ] **Step 2: Run policy tests and verify RED**

Run `PYTHONPATH=src ./.venv/bin/pytest -q tests/unit/analysis/test_audit_model_policy.py` and confirm import/behavior failure is caused by the missing policy implementation.

- [ ] **Step 3: Implement the minimal immutable policy module**

Use a frozen dataclass with context tokens, output tokens, safety ratio, concurrency, split depth, and minimum segment count. Match provider/model names case-insensitively and fall back conservatively.

- [ ] **Step 4: Verify GREEN**

Run the policy test file and then the existing provider/model-selection tests.

- [ ] **Step 5: Write failing budget-aware partition tests**

Add tests proving identical transcripts yield more chunks under the conservative unknown-model policy than GLM/Kimi policies, and that fixed Prompt growth reduces transcript capacity without producing an empty chunk.

- [ ] **Step 6: Implement budget-aware partitioning**

Extend `partition_transcript_for_audit` to accept a computed safe input budget while retaining the current explicit `max_markdown_chars` compatibility path for old callers/tests.

- [ ] **Step 7: Verify partition GREEN and commit**

Run policy, partition, composer, and single-report tests. Commit only Task 3 files with `feat: size report audit chunks by model capability`.

---

### Task 4: 实现审计块持久化与截断二分恢复

**Files:**
- Modify: `backend/src/audio_memory/analysis/segmented_report_audit.py`
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Modify: `backend/tests/unit/analysis/test_segmented_report_audit.py`
- Modify: `backend/tests/integration/test_single_report_runner.py`

**Interfaces:**
- Produces: stable audit chunk IDs, resumable chunk-result map in `staged_results_json`, bounded split tree.
- Consumes: `ProviderAnalysisError.code`, model policy, current report/prompt fingerprints.

- [ ] **Step 1: Write a failing stable-ID test**

Use literal segment IDs and two prompt fingerprints. Assert identical inputs return the same ID, while a changed report/prompt fingerprint changes it.

- [ ] **Step 2: Verify RED, implement stable IDs, verify GREEN**

Hash canonical JSON containing report fingerprint, prompt fingerprint, policy version, first segment ID, last segment ID, and segment count.

- [ ] **Step 3: Write a failing integration test for partial success**

Use a deterministic fake provider: chunk A succeeds, chunk B raises `ProviderAnalysisError(code="model_output_truncated")`, B-left and B-right succeed. Assert A is called once, only B is split, and merged coverage equals the server-known total.

- [ ] **Step 4: Verify RED and implement bounded split recovery**

Catch only truncation for binary splitting. Persist each validated leaf result immediately through the existing checkpoint save path. Keep network timeout retries on the same leaf and propagate authentication/content/schema failures.

- [ ] **Step 5: Write a failing restart-resume test**

Stop after one validated leaf is persisted, construct a new runner/provider, rerun the version, and assert the saved leaf is not requested again.

- [ ] **Step 6: Implement fingerprint-gated resume and verify GREEN**

Reuse only validated leaf payloads whose stable IDs and fingerprints match. Invalid or malformed saved leaves must be discarded and regenerated.

- [ ] **Step 7: Commit resumable audit**

Run all report audit and single-report integration tests. Commit with `feat: resume and split truncated report audit chunks`.

---

### Task 5: 修正审计失败的任务状态

**Files:**
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Modify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `backend/tests/integration/test_single_report_runner.py`
- Modify: `backend/tests/unit/analysis/test_task_coordinator.py`
- Modify: `prototype/tests/api-state.test.mjs`
- Modify: `prototype/tests/product-state.test.mjs`

**Interfaces:**
- Produces: recoverable `report_generated_audit_pending` state and retry entry point that preserves V1 and successful audit leaves.

- [ ] **Step 1: Write failing backend state tests**

Assert exhausted audit splitting keeps the generated V1 checkpoint but does not set analysis/job status to `completed`; retry must reuse V1 and successful audit leaves.

- [ ] **Step 2: Verify backend RED and implement the recoverable state**

Represent audit-pending as a nonterminal/retryable state with an explicit error code; do not route it through `VersionPublisher.publish`, which unconditionally marks the version completed.

- [ ] **Step 3: Write failing frontend state tests**

Assert the UI label is `报告已生成，审计待重试`, exposes retry, and never displays it as full completion.

- [ ] **Step 4: Verify frontend RED, implement UI mapping, verify GREEN**

Run focused Node tests, then backend task-coordinator and single-report tests.

- [ ] **Step 5: Commit status correction**

Commit Task 5 files with `fix: keep incomplete report audits retryable`.

---

### Task 6: 审查并合入分析期间防休眠开关

**Files:**
- Cherry-pick unique commit: `92a07f3`
- Verify: `backend/src/audio_memory/power/sleep_prevention.py`
- Verify: `backend/src/audio_memory/api/settings.py`
- Verify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Verify: `prototype/src/App.jsx`
- Verify: `backend/tests/unit/test_sleep_prevention.py`
- Verify: `backend/tests/integration/test_settings_api.py`

**Interfaces:**
- Produces: persisted `prevent_sleep` setting and reference-counted task leases.

- [ ] **Step 1: Cherry-pick only the unique sleep-prevention commit**

Resolve conflicts against Tasks 2–5, retaining every success, failure, cancellation, and shutdown release path.

- [ ] **Step 2: Run focused tests**

Run sleep manager, settings API, task coordinator, upload API, and local-web-security tests.

- [ ] **Step 3: Audit lifecycle safety**

Verify duplicate acquisition is idempotent, concurrent jobs keep the assertion alive, last release terminates it, unsupported platforms fail open, and subprocess handles cannot leak after shutdown.

- [ ] **Step 4: Commit conflict resolution if needed**

If cherry-pick conflict resolution changes behavior, commit those tested resolutions separately as `fix: preserve sleep prevention across report recovery`.

---

### Task 7: 审查并合入平滑转写进度

**Files:**
- Cherry-pick unique commit: `92feabf`
- Verify: `backend/src/audio_memory/transcription/eta.py`
- Verify: `backend/src/audio_memory/uploads/service.py`
- Verify: `prototype/src/store.js`
- Verify: `prototype/src/App.jsx`
- Verify: `backend/tests/unit/transcription/test_eta.py`
- Verify: `backend/tests/integration/test_upload_jobs.py`

**Interfaces:**
- Consumes: persisted real checkpoint progress and elapsed-time observations.
- Produces: monotonic display progress capped below stage completion.

- [ ] **Step 1: Cherry-pick only the unique smooth-progress commit**

Resolve UI conflicts while retaining Task 5 state semantics and actual-model labels.

- [ ] **Step 2: Run ETA, upload-job, API-state, product-state, and detail-layout tests**

- [ ] **Step 3: Audit truthfulness**

Verify refresh restores progress from server state, displayed progress never decreases, interpolation never crosses the next real checkpoint, and report text generation never displays 100%.

- [ ] **Step 4: Commit tested conflict resolutions if needed**

Use `fix: preserve truthful progress across restored jobs`.

---

### Task 8: 合入 5 分钟 Whisper 物理分块与块级检查点

**Files:**
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Create: `backend/src/audio_memory/transcription/physical_checkpoints.py`
- Modify: `backend/tests/unit/transcription/test_segments.py`
- Create: `backend/tests/unit/transcription/test_physical_checkpoints.py`
- Modify: `backend/tests/integration/test_transcription_recovery.py`

**Interfaces:**
- Produces: shifted Whisper timestamps and atomically persisted raw subchunk results keyed by batch/audio/model/parameter fingerprints.
- Consumes: fixed `WHISPER_CHUNK_SECONDS = 300`, compact batch, staging path registry.

- [ ] **Step 1: Write failing timestamp-shift tests**

Assert segment and word timestamps receive the exact physical-block offset without mutating the provider's original dictionaries.

- [ ] **Step 2: Verify RED, move the verified helper into the integration branch, verify GREEN**

- [ ] **Step 3: Write failing checkpoint round-trip tests**

Use a temporary staging directory and literal raw Whisper payload. Assert atomic save/load, stable fingerprint matching, rejection of changed model/parameters/audio, and safe handling of malformed JSON.

- [ ] **Step 4: Verify RED and implement `physical_checkpoints.py`**

Use write-to-sibling-temp plus `Path.replace`; validate all paths through the existing staging-path fence. Store only raw results and metadata, never formal transcript rows.

- [ ] **Step 5: Write failing mid-batch recovery integration test**

Simulate three physical chunks, persist chunks 0 and 1, interrupt before chunk 2, restart, and assert only chunk 2 invokes Whisper while the final mapped transcript matches a clean uninterrupted run.

- [ ] **Step 6: Verify RED and integrate recovery into `MLXWhisperEngine`**

Preserve the current compact checkpoint fingerprint. Delete physical checkpoint artifacts only after the compact batch is mapped, deduplicated, persisted, and its existing checkpoint advances.

- [ ] **Step 7: Run transcription regression tests and commit**

Run segment, checkpoint, mapping, compact, transcription recovery, diarization, and upload tests. Commit with `feat: resume five minute Whisper subchunks`.

---

### Task 9: 挑选当日 UI 与运行日志修复

**Files:**
- Modify: `prototype/src/store.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/tests/product-state.test.mjs`
- Modify: `prototype/tests/detail-layout.test.mjs`
- Verify: `backend/src/audio_memory/transcription/engine.py`

**Interfaces:**
- Produces: actual provider/model display name, hidden percentage during report text generation, per-subchunk timing logs.

- [ ] **Step 1: Confirm existing tests fail on the integration branch**

Port only the already-written product/detail assertions for GLM 5.2 labeling and no 100% during report generation. Run them before production changes and record RED.

- [ ] **Step 2: Port the minimal UI implementation and verify GREEN**

Do not copy unrelated root-worktree `App.jsx` or `store.js` changes.

- [ ] **Step 3: Verify subchunk logging behavior through a real test boundary**

Extend the recovery integration test to capture logs and assert start/completion records identify batch, part count, audio seconds, elapsed seconds, and real-time factor.

- [ ] **Step 4: Run frontend tests/build and commit**

Commit with `fix: show truthful model and report generation progress`.

---

### Task 10: 全量回归、差异审查与合入 main

**Files:**
- Review: all changes from `main..codex/adaptive-audit-main-integration`
- Update: `docs/working/2026-08-18-adaptive-audit-integration-baseline.md`

**Interfaces:**
- Produces: verified clean integration branch and fast-forwarded `main`.

- [ ] **Step 1: Run complete backend tests**

Run `PYTHONPATH=src ./.venv/bin/pytest -q` from `backend` without provider credentials.

- [ ] **Step 2: Run complete frontend verification**

Run `npm test`, `npm run build`, and the offline Playwright recovery/sleep/progress specs that do not require live providers.

- [ ] **Step 3: Run static and artifact checks**

Run `git diff --check`, inspect `git status --short`, and verify the diff contains no database, `.private-eval`, `.vite`, `.playwright-cli`, `outputs`, credentials, or temporary Whisper artifacts.

- [ ] **Step 4: Review every integration commit**

Check behavior, security boundaries, cancellation cleanup, checkpoint compatibility, retry ceilings, cost amplification, and UI truthfulness. Any defect gets a failing regression test before correction.

- [ ] **Step 5: Record final evidence**

Append exact test commands/counts, reviewed commits, remaining limitations, and rollback point to the baseline document; commit as `docs: record adaptive audit integration verification`.

- [ ] **Step 6: Merge into main**

After all checks pass, update the `main` worktree by fast-forward merge from `codex/adaptive-audit-main-integration`. Re-run a focused smoke suite on `main` and confirm both worktrees are clean.
