# 交接文档：风险门合并与回归测试续接

更新日期：2026-08-07  
工作目录：`/Users/liujinxin/Documents/音频Always on Demo`

## 当前结论

“快速转写风险门”开发已经完成、合并到 `main`，并通过合并后完整验证。当前本机产品已经迁移到数据库版本 `0010`，历史业务数据和 API Key 均已清除，Prompt 保留，适合从“配置 API Key”开始进行人工回归测试。

## 当前分支与提交

- 当前分支：`main`
- 当前 HEAD：`066ba85 merge: harden transcription risk gate`
- 风险门最后修复提交已包含在 main：`360a037 fix: retain isolated repetition evidence`
- 合并前的完整功能分支仍保留：`codex/transcription-phase0`
- 风险门工作树：`/Users/liujinxin/Documents/音频Always on Demo/.worktrees/codex-transcription-phase0`

不要再次把 `codex/transcription-phase0` 合并到 main；它已经合并完成。工作树暂时必须保留，因为其中有受保护的未跟踪基准脚本。

## 已完成功能

风险门已实现并合入：

- 全量快速转写固定使用句段级时间戳（`word_timestamps=False`）。
- 不可信片段在持久化时清空文本与词级时间戳，无法进入分析、卡片、问答、重分析等下游。
- VAD 原始语音区间、处理 padding、能量桶分别持久化；风险判定只使用原始 VAD 区间并只施加一次 300ms 容差。
- 文件边界越界、不可消解时间冲突、空文本、无 VAD 支持等硬拒绝。
- 近似重复、长静音后重复、异常语速等风险判定；超长或计算预算不足时保守隔离，同时仍仅作为内存风险证据参与后续 30 秒窗口判断。
- 每段最多一次词级精转写；精转写后仅文本复检。
- 队列上限为 `max(10, ceil(总片段数 × 5%))`，风险门总增量受快速转写耗时 20% 墙钟预算限制。
- 分类中断/恢复安全、旧数据库升级后的未复核文本下游隔离、日志脱敏、离线校准工具与结构化指标。
- 新增迁移：`0008`、`0009`、`0010`。

## 最终验证证据

在合并后的 `main` 上执行并通过：

- 后端：`cd backend && env UV_CACHE_DIR='../.uv-cache' uv run pytest -q`  
  结果：**543 passed**。
- 前端：`cd prototype && node --test tests/*.test.mjs`  
  结果：**47 passed**。
- 前端生产构建：`cd prototype && npm run build`  
  结果：通过。

此前风险门专项、迁移、恢复、脱敏、离线校准与最终范围复核均已完成。没有执行真实模型调用或真实外部评测。

## 本机测试环境

- 服务地址：<http://127.0.0.1:8765/>
- 当前服务进程：PID `35740`（记录时）
- 健康检查：`GET /api/health` 返回正常。
- 本机数据库：`~/Library/Application Support/AudioMemory/audio-memory.sqlite3`
- 数据库版本：`0010`
- 当前历史：已清空。
- 当前 API Key：Kimi、DeepSeek、OpenAI 均为未配置；钥匙串中的 DeepSeek Key 已删除，另外两项原本不存在。
- Prompt：**保留**，不得清除。

如服务未运行，可在项目根目录执行：

```bash
./scripts/start.sh
```

如需从全新用户流程回归：先在页面“模型与 API Key”完成配置，再上传音频。

## 人工回归建议

建议依次验证：

1. API Key 配置、重新校验、切换厂商。
2. MP3/AAC 上传、取消、失败恢复、历史清除。
3. 常规音频快速转写与分析发布。
4. 长音频的进度、预计耗时、VAD/说话人分段、取消与重试。
5. 卡片详情、问答、待办编辑/完成/删除、历史、Prompt 编辑与历史重分析。
6. 风险门边界：不可信转写不能出现在卡片、问答或分析结果中；正常音频不应被过度过滤。

真实模型评测仍需用户明确授权后才可执行；当前只做了离线合成校准，未将合成数据作为生产阈值证据。

## 受保护或无关文件

主工作目录中以下未跟踪文件属于用户/既有工作，不能删除、暂存或覆盖：

- `.playwright-cli/`
- `.superpowers/brainstorm/`
- `prototype/src/mockEngine.js`
- `prototype/tests/mock-engine.test.mjs`

风险门工作树中以下未跟踪文件受保护，不能删除、暂存或执行：

- `scripts/benchmark-local-transcription.py`

## 独立演示页分支（不要误合并）

此前曾错误启动一个独立展示页方向，后已停止。它不属于当前产品演示方案，且没有合入 main：

- 分支：`codex/founder-memory-demo`
- 工作树：`/Users/liujinxin/Documents/音频Always on Demo/.worktrees/codex-founder-memory-demo`
- 状态：Task 1–3 有提交，Task 4 未完成；另有未跟踪 `demo-dashboard/README.md`、`demo-dashboard/isolation.test.mjs`。

除非用户明确重新要求独立展示页，否则不要继续、合并或清理此分支。

## 新窗口续接指令

```text
请阅读 docs/HANDOFF-2026-08-07-CONTINUATION.md 后继续。

当前 main 已合并快速转写风险门，后端 543 项、前端 47 项、生产构建均已通过。本机服务运行在 http://127.0.0.1:8765/，数据库已迁移到 0010，历史与 API Key 已清空、Prompt 保留。下一步是用户进行从 API Key 配置开始的人工回归测试；只在用户报告问题后诊断和修复。

不要再次合并 codex/transcription-phase0；不要删除/暂存受保护的未跟踪文件；不要继续或合并 codex/founder-memory-demo，除非用户明确要求。真实模型调用需单独明确授权。
```
