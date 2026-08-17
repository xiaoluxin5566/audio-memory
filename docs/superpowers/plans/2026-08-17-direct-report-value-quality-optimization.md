# 全天报告价值与质量优化实施方案

> **供执行 Agent 使用：** 实施时必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项完成。所有步骤使用复选框（`- [ ]`）跟踪。

**目标：** 在不重新引入场景底图压缩、也不削弱事实安全的前提下，提高标题自然度、内容可读性、知识价值、分析深度和建议适配度。

**架构：** 保留“完整逐字稿 → 完整 V1 → 分段并行审核 → 合并审核 → 唯一一次定向修改 → 有界终审”的现有顺序。在现有审核与合并调用中增加有证据约束的“价值提升机会”，允许对明确选中的章节进行一次有界重写，未选中的 V1 章节必须原样保留；Markdown 编号问题由前端解析器确定性修复。

**技术栈：** Python 3、Pydantic v2、Markdown Prompt、pytest、React、JavaScript、Node test runner。

**依据：** `docs/HANDOFF-2026-08-17-PARALLEL-SEGMENTED-REPORT-AUDIT.md`，以及本文“全局约束”中已经确认的设计决策。

## 全局约束

- 第一份完整报告继续直接读取完整逐字稿。场景底图、分段摘要、专项 memo 或主题候选列表都不能替代完整逐字稿。
- 不新增模型调用次数，只重新分配现有分段审核、合并、修改和终审调用的职责。
- 分段审核只能发现局部证据、问题和价值机会，不能重新定义全天主题集合。
- 直接引语要求近似逐字一致；普通转述只要求语义准确；合理推断在前提真实、推理合理、确定性与证据匹配时允许存在。
- 通用知识可以解释已经选中的主题，但不能被写成关于读者个人状态的事实。
- 每个被选入报告的重要主题都必须提供超出逐字稿复述的新价值。根据主题性质，这种价值可以是概念解释、运行机制、重要性、关键取舍、适用边界或可复用的判断框架；不能只对健康或内容消费进行深化。
- 不机械要求每个主题都补充“概念解释”。个人事件更需要重要性和取舍分析；专业概念、产品、公共信息和内容消费更需要定义、机制、差异和判断框架。
- 审核建议前先判断建议是否应该存在。正确地不给建议不能被扣分。
- 分析深度按适配度判断，更深不自动代表更好。
- 未修改的 V1 章节必须逐字节保持不变；终审不能触发第二次修改。
- 未经用户重新授权，不调用真实模型、不访问 Keychain、不覆盖历史输出目录。
- 保留当前脏工作区，只暂存当前任务涉及的文件。

---

### 任务一：建立自然标题与自适应 Markdown 结构规则

**文件：**
- 修改：`backend/src/audio_memory/prompts/direct-report-generation.md`
- 测试：`backend/tests/unit/prompts/test_direct_report_prompt.py`

**接口：**
- 输入：完整逐字稿 Markdown。
- 输出：一份完整的 V1 Markdown 报告。

- [ ] 增加失败测试，确认生成请求包含以下规则：

```python
assert "标题只表达当天最重要的一项进展、矛盾或判断" in request.instructions
assert "不要为了覆盖全文而拼接两个不相关主题" in request.instructions
assert "结构服务内容" in request.instructions
assert "每一点需要较长解释时" in request.instructions
assert "合理推断" in request.instructions
assert "通用知识" in request.instructions
```

- [ ] 运行 `cd backend && ./.venv/bin/pytest -q tests/unit/prompts/test_direct_report_prompt.py`，确认修改 Prompt 前测试失败。
- [ ] 将现有“行动总结型标题”改为只表达一个核心判断的编辑型标题，删除强行拼接工作与亲子主题的示例。
- [ ] 增加条件式结构选择规则：

```text
连续事件推进 → 自然段落
2–5 个独立判断 → 项目符号
有前后依赖的步骤 → 编号列表
3 项以上稳定比较维度 → 表格
每一点需要较长解释 → ### 小标题
```

- [ ] 禁止在长段落中用“第一、第二、第三”或“其一、其二”隐藏可独立展示的内容；明确先完成事实和分析，再选择展示结构。
- [ ] 区分可核验事实、语义转述、合理推断和通用知识；只有直接引语要求近似逐字一致。
- [ ] 重新运行聚焦测试，确认通过。
- [ ] 只提交本任务两个文件，提交信息：`feat: refine direct report writing contract`。

---

### 任务二：把“价值提升机会”与“错误问题”分开建模

**文件：**
- 修改：`backend/src/audio_memory/prompts/direct_report_audit_schema.py`
- 修改：`backend/src/audio_memory/prompts/direct_report_revision_schema.py`
- 测试：`backend/tests/unit/prompts/test_direct_report_audit_schema.py`
- 测试：`backend/tests/unit/prompts/test_direct_report_revision_schema.py`

**接口：**
- 输出：`ReportAudit.value_opportunities`。
- 下游使用者：合并审核、定向修改和终审。

- [ ] 为以下接口增加失败测试：

```python
class AuditValueOpportunity(_StrictModel):
    opportunity_id: str
    kind: Literal[
        "knowledge_enrichment",
        "analysis_deepening",
        "advice_rework",
        "structure_rewrite",
        "title_rewrite",
    ]
    section_id: str
    current_gap: str
    desired_value: str
    evidence_segment_ids: list[str]
    evidence_excerpts: list[EvidenceExcerpt]
    preserve_constraints: list[str]
    allow_section_rewrite: bool
```

- [ ] 测试重复机会 ID、缺少目标章节、以及没有证据包却要求重大重写时必须被拒绝。
- [ ] 在 `ReportAudit` 中加入 `value_opportunities`，但不将其放入 `unresolved_issue_ids`；可选的深化机会不能机械导致报告不通过。
- [ ] 在定向修改映射中加入 `opportunities_resolved`，校验每个引用都存在且属于同一章节。
- [ ] 运行 `cd backend && ./.venv/bin/pytest -q tests/unit/prompts/test_direct_report_audit_schema.py tests/unit/prompts/test_direct_report_revision_schema.py`，确认通过。
- [ ] 提交本任务文件，提交信息：`feat: model report value opportunities`。

---

### 任务三：校准事实审核，并把现有合并调用升级为“主编”

**文件：**
- 修改：`backend/src/audio_memory/prompts/direct-report-audit.md`
- 修改：`backend/src/audio_memory/prompts/composer.py`
- 测试：`backend/tests/unit/prompts/test_direct_report_prompt.py`

**接口：**
- 分段调用输入：完整 V1 加当前逐字稿分段。
- 现有合并调用输出：原子问题以及章节级价值提升机会。

- [ ] 增加失败测试，要求 Prompt 包含“普通转述不要求逐字一致”“合理推断”“结论强度”“价值提升机会”“不重新定义全天主题”“过度分析”和“过度建议”。
- [ ] 按内容类型重写事实审核规则：

```text
直接引语 → 近似逐字一致
可核验事实 → 严格检查身份、归因、金额、时间、关系和结果
普通转述 → 保持语义准确
合理推断 → 前提有证据、推理合理、确定性适配
通用知识 → 可以补充，但不能无证据个性化
行动建议 → 可以由模型提出，但必须先通过相关性和安全门槛
```

- [ ] 明确禁止仅因为分析句没有在逐字稿中逐字出现就扣分。
- [ ] 将 0–4 深度阶梯替换为适配状态：`not_applicable`、`insufficient`、`adequate`、`appropriately_deep`、`overanalysis`；只有 `insufficient` 和 `overanalysis` 产生扣分。
- [ ] 将建议审核拆成两步：先判断适用性（`not_applicable`、`optional`、`necessary`），再判断质量（`unqualified`、`qualified`、`high_value`、`overprescribed`）。
- [ ] 要求分段审核检查每个重要主题是否提供了超出复述的价值，并在必要时输出局部价值机会。不同主题可以要求概念解释、机制、重要性、取舍、边界、判断框架或删除建议；不得只检查黄精、健康、媒体或内容消费，也不得强迫所有主题使用同一种深化方式。
- [ ] 将现有合并调用升级为全局主编：检查标题、全文主次、深度适配、建议适用性和结构，同时仍只输出审核与编辑任务包。
- [ ] 确保合并调用永远不输出新的场景底图或替代版完整报告。
- [ ] 重新运行聚焦 Prompt 测试，确认通过。
- [ ] 提交本任务文件，提交信息：`feat: audit report value and calibrated inference`。

---

### 任务四：把“最小改字”升级为唯一一次有界价值重写

**文件：**
- 修改：`backend/src/audio_memory/prompts/direct-report-revision.md`
- 修改：`backend/src/audio_memory/prompts/composer.py`
- 修改：`backend/src/audio_memory/analysis/direct_report_pipeline.py`
- 测试：`backend/tests/unit/prompts/test_direct_report_prompt.py`
- 测试：`backend/tests/unit/analysis/test_direct_report_pipeline.py`

**接口：**
- 输入：V1 章节大纲、可修改章节、合并问题、价值机会和规范化证据包。
- 输出：由未修改原章节与定向重写章节拼接而成的唯一 V2。

- [ ] 增加失败测试：修改请求能收到 `opportunity_knowledge_selected_topic` 及其保留约束，但不能收到完整逐字稿。
- [ ] 增加双章节测试：只修改一个章节，另一个章节必须逐字节保持不变。
- [ ] 用以下规则替换笼统的“最小修改”：

```text
未选中的章节必须原样保留。对于明确选中的章节，使用能够交付目标价值的最小范围重写；只有 allow_section_rewrite=true 时才允许整章重写。不得重新选择全天主题，也不得重新生成整篇报告。
```

- [ ] 只传递目标为可编辑章节的价值机会；证据型机会携带规范化逐字稿摘录，标题和结构机会携带报告章节摘录。
- [ ] 拒绝未知机会、跨章节兑现、证据越界、未经授权的整章重写，以及未修改章节丢失。
- [ ] 运行 `cd backend && ./.venv/bin/pytest -q tests/unit/prompts/test_direct_report_prompt.py tests/unit/analysis/test_direct_report_pipeline.py`，确认通过。
- [ ] 提交本任务文件，提交信息：`feat: add bounded value revision`。

---

### 任务五：保持终审有界且禁止第二次修改

**文件：**
- 修改：`backend/src/audio_memory/prompts/direct-report-audit.md`
- 修改：`backend/src/audio_memory/prompts/composer.py`
- 测试：`backend/tests/unit/prompts/test_direct_report_prompt.py`
- 测试：`backend/tests/integration/test_audited_single_report_runner.py`

**接口：**
- 输入：V2、原始问题与价值机会、修改兑现映射和变更章节清单。
- 输出：评分以及发布/降级状态，不输出新的修改请求。

- [ ] 增加失败测试，要求终审收到价值机会兑现映射、使用校准后的推断规则，并明确禁止第二次修改。
- [ ] 要求终审核验：目标价值是否兑现，以及是否引入无依据的个性化、新行动膨胀、标题冲突或 Markdown 损坏。
- [ ] 保持终审有界：不得声称重新完整审阅逐字稿，不重新发现全天内容。
- [ ] 运行聚焦单元测试和集成测试，确认通过。
- [ ] 提交本任务文件，提交信息：`feat: verify report value in final audit`。

---

### 任务六：确定性修复多段编号列表

**文件：**
- 修改：`prototype/src/api/state.js`
- 修改：`prototype/src/App.jsx`
- 测试：`prototype/tests/api-state.test.mjs`

**接口：**
- 输入：编号标题与解释段落之间存在空行的 Markdown。
- 输出：一个保留序号和附属解释的有序列表块。

- [ ] 增加截图对应的回归测试：三个编号建议分别跟随一个解释段落，解析结果必须是同一个列表，序号为 `[1, 2, 3]`。
- [ ] 运行 `cd prototype && node --test tests/api-state.test.mjs`，确认当前解析器把列表拆开并导致测试失败。
- [ ] 将每个编号项解析为：

```javascript
{
  ordinal: 1,
  text: '**辅导前先设时间上限。**',
  continuation: ['数学辅导控制在 15–20 分钟。'],
}
```

- [ ] 跨解释段落继续收集同一个有序列表，直到遇到下一个同级编号、标题、表格、项目列表或输入结束。
- [ ] 渲染为一个 `<ol>`，每个解释段落留在所属 `<li>` 内，同时兼容旧的字符串列表项。
- [ ] 运行 `cd prototype && node --test tests/api-state.test.mjs tests/detail-layout.test.mjs`，确认通过。
- [ ] 提交本任务文件，提交信息：`fix: preserve ordered report list numbering`。

---

### 任务七：增加离线回归与真实评测门槛

**文件：**
- 修改：`backend/tests/unit/prompts/test_direct_report_prompt.py`
- 修改：`backend/tests/unit/prompts/test_direct_report_audit_schema.py`
- 修改：`backend/tests/unit/analysis/test_segmented_report_audit.py`
- 修改：`backend/tests/unit/analysis/test_direct_report_pipeline.py`
- 修改：`backend/tests/integration/test_single_report_runner.py`
- 修改：`backend/tests/integration/test_audited_single_report_runner.py`
- 修改：`prototype/tests/api-state.test.mjs`

**接口：**
- 输出：防止场景底图回归、字面主义审核、建议膨胀、整篇重写和编号重置的离线保护。

- [ ] 增加生成测试：第一次请求仍然接收完整逐字稿，不能接收审核摘要、价值机会或场景底图。
- [ ] 增加合理推断样例：“有点记不出了”可以支持“可能难以继续处理新的解释”，但不能支持确定诊断。
- [ ] 增加“不给建议也是正确结果”样例：只有汽车发布会、没有读者购车需求时，不产生行动建议和行动性扣分。
- [ ] 增加跨领域价值样例：用黄精验证“概念解释但不进行健康个性化”；用 AI/产品主题验证机制与差异；用职业决策验证取舍和可复用判断框架。任何样例都不能变成硬编码必写类别。
- [ ] 运行完整离线测试：

```bash
cd backend
./.venv/bin/pytest -q tests/unit tests/integration/test_single_report_runner.py tests/integration/test_audited_single_report_runner.py
cd ../prototype
node --test tests/*.test.mjs
```

- [ ] 运行 `git diff --check` 并检查 `git status --short`，确认没有空白错误，也没有暂存无关文件。
- [ ] 提交回归测试文件，提交信息：`test: guard direct report value quality`。

---

## 实施后的真实评测门槛

不得自动调用 DeepSeek。离线测试通过后，重新向用户申请授权；获得授权后：

1. 保留所有历史输出目录，创建新的评测目录。
2. 复用 7 月 29 日和 30 日的现有 V1，只运行分段审核、合并、一次价值修改和终审。
3. 记录任务是全新还是续跑、调用次数、Token 和各阶段墙钟耗时。
4. 使用已经确认的 100 分标准人工评分。
5. 必须同时满足：
   - V1 中的重要主题没有被静默删除；
   - 只有媒体内容、没有真实需求时，不产生购买、试用、饮食、医疗或职业行动；
   - 合理转述与推断不因缺少原文同句而被拒绝；
   - 每个被选中的重要主题都以适合该主题的方式提供超出复述的新价值，包括概念解释、机制、重要性、取舍、边界或可复用判断框架；
   - 标题只表达一个自然的核心判断；
   - 较长的独立内容正确使用小标题、项目符号或表格；
   - 有序列表在网页上显示为 `1、2、3……`；
   - 人工评分高于当前 73/100 和 62/100，且重要内容覆盖不下降。
6. 如果任何一天丢失了 V1 的重要内容，应否决方案，不能继续增加修复轮次。
