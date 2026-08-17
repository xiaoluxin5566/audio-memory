# Audio Memory 分段并行审核与报告质量优化交接

更新时间：2026-08-17
工作目录：`/Users/liujinxin/Documents/音频Always on Demo`
当前分支：`codex/report-audit-revision-pipeline`

## 新窗口开场指令

复制下面这段给新窗口：

> 继续 `/Users/liujinxin/Documents/音频Always on Demo` 的全天报告质量优化。先完整阅读 `docs/HANDOFF-2026-08-17-PARALLEL-SEGMENTED-REPORT-AUDIT.md`，再检查当前工作区，不要覆盖或清理已有未提交修改。当前主线是：动态分段并行审核 → 合并原子问题 → 唯一一次定向修改 → 终审只判断、不再修改。先人工复核 7 月 29、30 日最新报告中仍存在的问题，再决定是否继续改 Prompt；不要只相信模型评分。

## 一、用户已确认的产品原则

1. 所有场景遵循同一原则：当天真实出现且有重点价值才写；未出现或没有重点就不生成、不扣分。
2. 工作、面试、亲子、内容消费等仅是重点扫描方向，不是必写模块。
3. 重点场景表示需要重点挖掘，不代表最终一定要写。
4. 报告必须像顾问面对面交流：自然、专业、明确，不展示分析过程。
5. 禁止文件编号、录音时段、分段、转写、置信度、筛选输入、模型链路等工程化表达。
6. 不建议用户改变、筛选或重新准备录音；只分析已经收到的内容。
7. 建议必须先判断是否应该存在，再追求具体；不能把媒体观点直接推成购买、医疗、职业或亲子行动。
8. 需要清单、材料、标准或话术时，直接给示例成品，并可注明“这是一个示例，供你参考”。
9. 内容消费应尽量提供知识、解释、延伸理解和真实推荐，而不是证明模型识别正确。
10. 三项以上稳定比较、建议或行动优先使用表格。
11. “数据范围与判断边界”不作为固定模块。
12. 终审发现问题后不再进行第二次修改；按状态展示报告。

## 二、当前链路

```text
生成完整 V1 Markdown
→ 按渲染后逐字稿长度动态分段
→ 所有分段共用同一 Prompt，并行审核（最多 6 路）
→ 合并审核：校验覆盖、去重、处理冲突、重新评分
→ 唯一一次定向局部修改
→ 终审只核验问题兑现和新问题，不再修改
→ 根据终审结果展示通过或降级状态
```

7 月 29 日被切成 5 段；7 月 30 日被切成 6 段。

## 三、最新 Prompt

- 生成：`backend/src/audio_memory/prompts/direct-report-generation.md`
- 分段审核、合并审核、终审：`backend/src/audio_memory/prompts/direct-report-audit.md`
- 定向修改：`backend/src/audio_memory/prompts/direct-report-revision.md`
- 审核 Schema：`backend/src/audio_memory/prompts/direct_report_audit_schema.py`
- Prompt 组装：`backend/src/audio_memory/prompts/composer.py`

### Prompt 中已经解决的关键冲突

- 分段审核的 `full_transcript_reviewed=false`，但当前分段 reviewed/total 必须相等。
- 一个 issue 只能对应一个章节和一处问题，`related_section_ids` 始终为空；跨章节同类问题拆成多个 issue。
- 事实错误、错误归因、过程泄露对应的原文必须删除或替换。
- 遗漏型问题允许保留原句，但必须在原位置补足审核要求的内容。
- 终审输入只包含 V2、原子问题摘要、修改兑现映射和变更章节清单，避免重复三份正文。
- 终审输出上限为 32K；只列仍未解决或新引入的问题。

## 四、代码实现位置

- 动态分段、并行调度、原子问题校验：
  `backend/src/audio_memory/analysis/segmented_report_audit.py`
- 主流程接线：
  `backend/src/audio_memory/analysis/single_report_runner.py`
- 证据清理、证据边界、修改兑现、状态元数据：
  `backend/src/audio_memory/analysis/direct_report_pipeline.py`
- DeepSeek 显式并发通道：
  `backend/src/audio_memory/analysis/provider.py`
- 真实评测脚本：
  `tests/real-audited-report-eval.py`
- 两天网页预览构建：
  `scripts/build-two-day-audited-preview.py`

### 并发行为

- 只有分段审核使用 `allow_parallel=True`。
- 生成、审核合并、定向修改、终审继续串行。
- 并发上限为 6。
- `ProviderAnalysisClient` 默认行为仍是串行，避免影响其他分析场景。

## 五、真实评测结果

### 7 月 30 日首次完整端到端跑

目录：`outputs/deepseek-audited-report/parallel-segmented-v2-2026-07-30/`

- 输入：7453 段，379173 字符。
- 生产调用数：10。
- 完整端到端耗时：687.18 秒。
- 生成：167.45 秒。
- 6 段并行审核墙钟：204.03 秒。
- 合并审核：132.56 秒。
- 定向修改：115.66 秒。
- 终审：67.49 秒。
- 六段单独耗时合计约 925 秒；并行后为 204 秒，分段审核等待降低约 78%。
- 首次终审 100 分，但人工复核发现“建议回听、今天的音频、下次用 GPT 分析”等漏审，因此不能把 100 当作最终质量结论。

### 7 月 30 日补强审核后的最终产物

- 报告：`outputs/deepseek-audited-report/parallel-segmented-v2-2026-07-30/report.md`
- 终审：`outputs/deepseek-audited-report/parallel-segmented-v2-2026-07-30/v2-final-audit.json`
- 模型评分：97。
- 终审通过，无 major，剩 1 个 minor。
- minor：英语课标题仍把“屏幕看不清”写成已确认，但正文明确说不能确认。
- 最新 `comparison.json` 是续跑终审口径，只记录终审 169.94 秒，不是完整端到端耗时；不要拿它替代上面的 687.18 秒首次完整跑数据。

### 7 月 29 日最终产物

目录：`outputs/deepseek-audited-report/parallel-segmented-v2-2026-07-29/`

- 报告：`report.md`
- 终审：`v2-final-audit.json`
- 模型评分：92。
- 终审未通过。
- 剩余 1 个 major：顶部概览仍把朋友的教育观点误归为媒体播放。
- 剩余 1 个 minor：顶部概览仍保留不准确的“脑子里一头雾水”引语。
- 按用户规则，终审后不再修改，因此保留失败状态。

## 六、人工复核结论：不要只看模型分数

7 月 30 日虽然自动终审为 97 分，仍建议继续关注：

1. 黄精部分把健康建议写得过于具体，例如直接把预算转成牛羊肉、蛋奶和蔬菜。
2. 电子产品、汽车部分仍容易从媒体内容推导购买或试驾行动，需先确认用户真实购买需求。
3. 职业独白身份无法确认，却占据较多篇幅并提出职业行动，价值与风险需重新权衡。
4. “无法确认的，不要写”仍像模型审查日志，不像顾问直接面对读者。
5. 英语课标题与正文存在终审已指出的轻微矛盾。

客观结论：分段审核提升了遗漏召回和局部问题定位，但模型终审仍会高估报告质量，必须保留人工抽检。

## 七、耗时与成本判断

- 新版不是纯性能优化。
- 旧版一次全量审核约 1 次调用；新版为 N 个分段审核 + 1 次合并。
- 7 月 30 日整条生产链为 10 次调用：生成 1 + 分段审核 6 + 合并 1 + 修改 1 + 终审 1。
- 首次完整跑总输入约 51.5 万 tokens，成本显著增加。
- 并行只降低多个分段之间的等待，无法减少生成、合并、修改和终审耗时。
- 如果下一步优化性能，应优先减少每段重复携带的 V1 报告内容，或采用报告章节索引/相关章节裁剪，但不能牺牲跨章节错误召回。

## 八、网页预览

两天预览数据：

`prototype/output/deepseek-historical-report-preview.json`

构建命令：

```bash
./backend/.venv/bin/python scripts/build-two-day-audited-preview.py
```

启动页面：

```bash
cd prototype
npm run dev -- --host 127.0.0.1
```

访问：

`http://127.0.0.1:5173/?reportPreview=deepseek`

当前信息流应显示：

- 2026-07-30：97 分，终审通过。
- 2026-07-29：92 分，终审未通过。

注意：若端口提示已占用，通常说明 Vite 已经运行，直接访问即可。若页面空白，先重新运行两天预览构建脚本，再刷新带 `reportPreview=deepseek` 的 URL。

## 九、验证状态

最后一次相关验证：

```bash
cd backend
./.venv/bin/pytest -q tests/unit tests/integration/test_single_report_runner.py tests/integration/test_audited_single_report_runner.py
```

结果：`748 passed in 6.78s`

`git diff --check` 通过。

## 十、工作区与安全边界

- 当前工作区非常脏，包含大量用户已有修改和未跟踪文件。
- 本任务没有提交、没有合并、没有清理工作区。
- 不要执行 `git reset --hard`、`git checkout --`、广泛 `git add .` 或删除 `outputs/`。
- 若需要提交，只选择本交接文档列出的相关文件逐个暂存。
- `outputs/` 中包含真实 DeepSeek 评测产物，不要覆盖历史目录。

本次直接相关文件包括：

```text
backend/src/audio_memory/analysis/provider.py
backend/src/audio_memory/analysis/single_report_runner.py
backend/src/audio_memory/analysis/direct_report_pipeline.py
backend/src/audio_memory/analysis/segmented_report_audit.py
backend/src/audio_memory/prompts/composer.py
backend/src/audio_memory/prompts/direct-report-generation.md
backend/src/audio_memory/prompts/direct-report-audit.md
backend/src/audio_memory/prompts/direct-report-revision.md
backend/src/audio_memory/prompts/direct_report_audit_schema.py
backend/tests/unit/analysis/test_provider.py
backend/tests/unit/analysis/test_direct_report_pipeline.py
backend/tests/unit/analysis/test_segmented_report_audit.py
backend/tests/unit/prompts/test_direct_report_audit_schema.py
backend/tests/unit/prompts/test_direct_report_prompt.py
backend/tests/integration/test_single_report_runner.py
backend/tests/integration/test_audited_single_report_runner.py
tests/real-audited-report-eval.py
scripts/build-two-day-audited-preview.py
prototype/output/deepseek-historical-report-preview.json
```

## 十一、建议的新窗口下一步

优先级建议：

1. 先人工逐段阅读 7 月 30 日最终报告，不要先改代码。
2. 把人工发现的问题与 `v2-final-audit.json` 对照，确认终审漏审模式。
3. 重点决定健康建议、媒体购买建议、身份不明重要场景的保留门槛。
4. 再修改生成/审核 Prompt，并只复用 V1 重跑审核链，避免每次重付生成成本。
5. 需要重新比较耗时时，务必做一次全新目录的完整端到端跑；续跑的 `comparison.json` 不能作为完整耗时。
