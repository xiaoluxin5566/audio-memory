# 2026-08-18 自适应审计集成基线

## 工作区

- 集成分支：`codex/adaptive-audit-main-integration`
- 起点：`main` / `14d715a`
- 原运行工作区：未修改
- 隔离工作树复用主工作区 Python 虚拟环境；该链接被 Git 忽略，不进入提交。

## 基线验证

- 后端：`681 passed in 8.08s`
- 前端 Node 测试：`79 passed, 0 failed`
- Vite 生产构建：通过，39 个模块完成转换。
- 首次前端执行的唯一失败为隔离工作树缺少 `backend/.venv/bin/python`；建立本地依赖链接后原测试通过，不是产品基线故障。

## 当日待合并改进

1. `codex/report-audit-revision-pipeline`
   - `5cdd954` 审计修订设计
   - `9b0cc91` 实施计划
   - `dfe9f8f` 审计报告质量链路
   - `a61234b` 审计 provider 作用域修正
2. `codex/analysis-sleep-prevention`
   - 唯一功能提交 `92a07f3`
3. `codex/smooth-progress`
   - 唯一功能提交 `92feabf`
4. 原工作区未提交、需逐行挑选的改进
   - 固定 300 秒 Whisper 物理分块、时间戳平移和子块运行日志
   - 显示实际 provider/model 名称
   - 报告文本生成期间不显示 100%

## 排除项

- `.private-eval/`, `.vite/`, `.playwright-cli/`, `outputs/`
- SQLite 数据库、临时音频、截图与真实报告
- Keychain/API 凭据
- 与本次主链路无关的历史实验代码

## 最终验证

待全部任务完成后追加实际命令、通过数、合入提交、限制与回滚点。
