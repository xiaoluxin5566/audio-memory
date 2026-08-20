# Version Integration Acceptance Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供一组可执行命令，从最新干净 main 安全启动、查看和停止指定 beta 版本的集成验收页面。

**Architecture:** 复用现有单所有者开发运行时，保持其已发布的所有者记录格式不变；集成验收使用独立、原子写入的元数据记录版本和 commit。新的验收控制器负责 main/commit 校验、活动任务门禁和安全交接；后端健康响应传递受控的页面标签。

**Tech Stack:** Python 3.12, Bash, FastAPI, React/Vite, pytest, Node test runner

**Spec:** `docs/superpowers/specs/2026-08-20-integration-acceptance-runtime-design.md`

## Global Constraints

- 正式版本 `8765` 和正式数据不得受影响。
- 集成验收只允许使用干净且精确指向 `main` 的 worktree。
- 开发页面固定 `5173`，开发后端固定 `8766`。
- 活动任务时不得自动切换或停止原环境。

---

### Task 1: 集成验收元数据与页面标签

**Files:**
- Modify: `backend/src/audio_memory/main.py`
- Modify: `prototype/src/api/client.js`
- Test: `backend/tests/unit/test_feature_runtime.py`
- Test: `backend/tests/integration/test_runtime_profile.py`
- Test: `prototype/tests/runtime-environment.test.mjs`

**Interfaces:**
- Produces: `AcceptanceRecord`, health `environment_label`.

- [ ] 先写独立元数据记录、受控健康标签和 UI 标签的失败测试。
- [ ] 运行聚焦测试，确认因新行为缺失而失败。
- [ ] 实现最小独立数据模型、环境传递和 UI 显示，并验证旧所有者记录仍可读。
- [ ] 重跑聚焦测试至全绿。

### Task 2: 集成验收安全交接控制器

**Files:**
- Create: `scripts/integration_runtime.py`
- Create: `scripts/integration-start.sh`
- Create: `scripts/integration-status.sh`
- Create: `scripts/integration-stop.sh`
- Test: `backend/tests/unit/test_integration_runtime.py`

**Interfaces:**
- Consumes: `RuntimeStore`, `RuntimeOwner`, `run_runtime`.
- Produces: `start_acceptance(version)`, `status_acceptance()`, `stop_acceptance()` 及三个用户命令。

- [ ] 先写版本格式、main 完全一致、活动任务拒绝、空闲自动交接、所有权停止和状态输出的失败测试。
- [ ] 运行聚焦测试，确认每个测试因相应生产行为缺失而失败。
- [ ] 实现校验、活动任务查询、经验证的 TERM 交接、启动及命令包装。
- [ ] 重跑聚焦测试至全绿。

### Task 3: 回归与真实切换

**Files:**
- Modify: `docs/working/2026-08-20-integration-acceptance-runtime.md`

**Interfaces:**
- Consumes: Task 1-2 的命令和标签。
- Produces: 可复查的测试与运行证据。

- [ ] 运行新增后端、前端和脚本聚焦测试。
- [ ] 运行现有功能运行时、开发隔离、前端全量和构建回归。
- [ ] 提交功能分支，通过功能门禁后合并到 `main`。
- [ ] 在无活动任务的前提下，用新命令切换当前开发页面，验证页面标签、健康接口、版本和 commit。
