# 首稿价值门槛与评分页脚 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让首次生成直接遵守审核与定向修订的价值标准，并在成品尾部可靠展示字数和定向修改增益。

**Architecture:** 将通用价值门槛前移到首次生成 Prompt；由运行时基于最终 Markdown 和已校验的审核对象生成页脚，不要求模型计数。内部保留定向终审的证据范围，用户页脚仅展示字数与增益。

**Tech Stack:** Python, Markdown, Pydantic, pytest

**Spec:** `docs/superpowers/plans/2026-08-17-direct-report-value-quality-optimization.md`

## Global Constraints

- 不重跑真实模型链路。
- 不覆盖或清理已有工作区修改。
- 字数与评分由服务端确定性计算。
- 定向终审在内部 Prompt 中必须明确有限证据范围；用户页脚不展示该说明。

---

### Task 1: 前移通用价值标准

**Files:**
- Modify: `backend/src/audio_memory/prompts/direct-report-generation.md`
- Test: `backend/tests/unit/prompts/test_direct_report_prompt.py`

- [x] 写失败测试，要求生成 Prompt 包含逐主题价值、知识增量、深度适配、建议存在性和反过度分析规则。
- [x] 运行定向测试并确认因缺少规则失败。
- [x] 将审核和修订中的通用标准改写为生成指令，不引入黄精等硬编码类别。
- [x] 运行定向测试确认通过。

### Task 2: 确定性报告页脚

**Files:**
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Test: `backend/tests/integration/test_audited_single_report_runner.py`

- [x] 写失败集成测试，要求 V1 成品显示字数和首次全量审核分，V2 成品显示“定向修改增益：首审分 → 定向终审分（差值）”。
- [x] 运行定向测试并确认失败原因是页脚缺失。
- [x] 实现 Markdown 正文字数统计和页脚生成，在发布前附加，且不将页脚自身计入字数。
- [x] 将 V1 审核分传入 V2 发布阶段，失败或未审核情况显示准确的降级文案。
- [x] 运行定向集成测试确认通过。

### Task 3: 终审口径正名与回归

**Files:**
- Modify: `backend/src/audio_memory/prompts/direct-report-audit.md`
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Test: `backend/tests/unit/prompts/test_direct_report_prompt.py`
- Test: `backend/tests/integration/test_audited_single_report_runner.py`

- [x] 写失败测试，要求终审 Prompt 禁止声称已重审全量逐字稿，评分范围使用“定向终审”语义。
- [x] 运行测试确认失败。
- [x] 补充 Prompt 和评分 scope 文案，保留现有 Schema 的 `full_transcript_reviewed=false` 强约束。
- [x] 运行 Prompt、集成与相关单元测试。
