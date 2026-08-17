# 2026-08-18 main 集成基线

## 基线

- 集成分支：`codex/report-main-integration`
- 起点：`main` / `85e01ffe59b674fe90486933a57c1fa48b019a2c`
- 原工作区分支：`codex/report-audit-revision-pipeline` / `a61234b5deabbd8476d90b4593eeb138995e2e35`
- 原工作区保持脏状态，本次只在 `.worktrees/report-main-integration` 中集成。
- 基线命令：`PYTHONPATH="$PWD/src" <原项目 venv>/pytest -q tests/unit tests/integration/test_single_report_runner.py`
- 基线结果：`9 failed, 584 passed`。
- 其中 8 个失败来自 `AutonomousAnalysisResult` 缺少 `todos`，1 个失败来自 `PromptComposer` 缺少 `compose_direct_report_light`。前者由契约修复层解决，后者由综合报告基础层解决。

## 综合报告基础层

- Prompt 与 Schema：`single-report.md`、direct report light/marked Schema、`composer.py` 的基础组合方法。
- 运行链路：`single_report_runner.py`、`direct_report_sections.py`、`markdown_report.py`、`direct_report_marked_document.py`。
- 验证：direct report Prompt、Markdown、light/marked Schema 和 single report runner 测试。
- 不包含审核、定向修改、GLM 或模型切换。

## 审核与修改层

- `direct_report_pipeline.py`、`segmented_report_audit.py`。
- audit/revision Prompt 与 Schema。
- `single_report_runner.py` 的 V1 审核、最多一次定向修改、定向终审和断点恢复。
- `provider.py` 仅加入审核场景的有限并行；GLM 注册留到供应商层。

## Prompt 与页脚层

- 首次生成 Prompt 全量包含知识增量、概念解释、判断框架、建议存在性、推断边界与 Markdown 结构选择。
- audit/revision Prompt 使用相同价值标准。
- `append_report_metrics()` 确定性生成字数与修改增益，且幂等。

## UI 与供应商层

- 报告 UI：质量元数据、Markdown 分隔线、连续编号列表、两天离线预览。
- 供应商 UI：GLM 配置、Kimi/DeepSeek/OpenAI/GLM 受控模型列表、模型切换 API 和持久化。
- 两层分别提交；最终验证 `provider.py` 同时保留审核并发与 GLM adapter。

## 跨层依赖

- `composer.py`：基础报告方法先落地，审核方法后落地。
- `single_report_runner.py`：V1 基础检查点先落地，审核状态机和页脚后落地。
- `markdown_report.py`：基础解析先落地，页脚函数后落地。
- `provider.py`：审核并发先落地，GLM 注册与模型选择后落地。
- `prototype/src/App.jsx`、`state.js`、`styles.css`：报告展示先落地，供应商配置 UI 后落地。

## 明确排除

- `.private-eval/`、`.vite/`、`.playwright-cli/`、数据库、真实 `outputs/` 和截图。
- 与本次综合报告或供应商配置无关的深度调查、重转写、历史日地图扩展和 MCP/搜索实验。
