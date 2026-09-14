# Beta 8 后续链路运行时 JSON 契约 V1

**状态：** 代码实现前的正式字段契约；具体 JSON Schema 由后端 Pydantic 模型生成并注入 Prompt。

## 1. 设计原则

1. 用户可见的实质内容始终保存在完整 Markdown 中；JSON 只负责路由、证据、审核、搜索、待办和恢复。
2. 模型不得生成或修改输入中的正式 ID。模型只允许生成 Schema 明确要求的本次输出临时键，例如 `decision_key`、`revision_task_key` 和 `search_task_key`；后端校验后再映射为稳定 ID。
3. 所有 `segment_id` 必须来自可靠逐字稿，所有 `source_id` 必须来自搜索适配器规范化后的外部来源。
4. 未知值使用 `null`，集合为空时使用空数组，不用空字符串、虚构默认值或省略必填字段。
5. 每个阶段只输出后续阶段真正需要的数据，不把模型思考过程、评分长表或用户可见正文拆成复杂 blocks。

## 2. 公共枚举

### `scene_id`

- `work_communication`
- `parenting_family`
- `health_state`
- `content_consumption`
- `inspiration_insight`
- `self_growth`
- `life_decisions`

### `audit_phase`

- `initial`
- `final`

### `audit_scope`

- `evidence_chunk`：核验当前逐字稿分段及其对应报告内容；
- `global_report`：检查完整卡片集合的跨卡关系和整体质量。

### `issue_type`

- `unsupported_fact`
- `distorted_fact`
- `wrong_attribution`
- `unsupported_inference`
- `quote_error`
- `missed_high_value_content`
- `wrong_card_boundary`
- `wrong_scene`
- `duplicate_content`
- `todo_error`
- `search_boundary_error`
- `external_source_error`
- `weak_help`
- `structure_or_style`

### `severity`

- `blocking`
- `important`
- `minor`

严重程度只决定修改顺序。所有有效问题都必须修正。

## 3. 七场景 V1 输出

七个场景仍使用已经确认的最薄输出：

```json
{
  "cards": [
    {
      "markdown": "完整用户可见 Markdown",
      "source_segment_ids": ["seg_0_12"],
      "search_candidates": [
        {
          "question": "需要核实的外部问题",
          "purpose": "它会改善什么判断或建议",
          "related_segment_ids": ["seg_0_12"]
        }
      ]
    }
  ],
  "todo_candidates": [],
  "skip_reason": null
}
```

后端校验后添加：

- `scene_result_id`：一次场景调用的稳定 ID；
- `scene_id`：当前场景；
- `card_id`：每张卡的稳定 ID；
- `search_candidate_id`：每个候选的稳定 ID；
- `todo_candidate_id`：每个待办候选的稳定 ID；
- `v1_markdown_sha256`：保护未受影响卡片原文。

以上字段由后端添加，不要求场景模型输出。

### Markdown 最小格式契约

这不是内容模板，只用于后端稳定提取列表页标题和核心摘要：

- `markdown` 的第一个非空行必须是一级标题 `# <标题>`；
- 标题之后、下一个 Markdown 标题之前必须存在非空的核心摘要；
- 核心摘要可以有一个或多个自然段，可以使用独立的“核心摘要”标签，但正文结构不固定；
- 后端将一级标题文本作为 `title`；将标题后、下一个标题前的内容去掉独立标签和 Markdown 标记后作为 `summary`；
- 缺少一级标题或核心摘要属于 Schema 后的运行时格式校验失败，进入同一次模型调用允许的一次结构修复，不增加新的业务阶段。

## 4. 统一审核输出

```json
{
  "audit_phase": "initial",
  "audit_scope": "evidence_chunk",
  "audit_unit_id": "audit-unit-0001",
  "input_complete": true,
  "input_error": null,
  "issues": [
    {
      "card_id": "card-0001",
      "suggested_scene_id": null,
      "issue_type": "wrong_attribution",
      "severity": "blocking",
      "problem": "把他人提出的方案写成了用户已经决定的方案。",
      "required_change": "恢复方案提出者，并把用户状态改为尚未回应。",
      "affected_excerpt": "你已经决定采用这套方案。",
      "evidence_segment_ids": ["seg_0_12", "seg_0_13"]
    }
  ],
  "revision_task_checks": [],
  "passed": false
}
```

字段约束：

- `audit_unit_id` 必须原样返回；
- `card_id` 必须引用输入卡片；只有 `missed_high_value_content` 可以为 `null`；
- `suggested_scene_id` 只在遗漏整张卡时填写；其他问题为 `null`；
- `affected_excerpt` 必须是最短必要连续原文；完全遗漏时为 `null`；
- `evidence_segment_ids` 只能引用当前审核单元提供的逐字稿；`global_report` 中没有逐字稿依据的问题为空数组；
- `input_error` 仅在输入截断、字段缺失或无法完成当前范围时填写；
- `passed=true` 必须同时满足 `input_complete=true`、`issues=[]`，并且终审中的所有修改任务均完成。

终审中的 `revision_task_checks` 元素：

```json
{
  "requirement_id": "req-0001",
  "completed": true,
  "reason": "正文已恢复真实提出者和用户尚未回应的状态。",
  "evidence_segment_ids": ["seg_0_12", "seg_0_13"]
}
```

## 5. 代码聚合后的审核包

这不是模型输出。后端在所有审核单元完成后形成：

```json
{
  "audit_phase": "initial",
  "expected_audit_unit_ids": ["audit-unit-0001", "audit-unit-global"],
  "completed_audit_unit_ids": ["audit-unit-0001", "audit-unit-global"],
  "incomplete_audit_unit_ids": [],
  "issues": [
    {
      "audit_issue_id": "issue-0001",
      "origin_audit_unit_ids": ["audit-unit-0001"],
      "card_id": "card-0001",
      "suggested_scene_id": null,
      "issue_type": "wrong_attribution",
      "severity": "blocking",
      "problem": "把他人提出的方案写成了用户已经决定的方案。",
      "required_change": "恢复方案提出者，并把用户状态改为尚未回应。",
      "affected_excerpt": "你已经决定采用这套方案。",
      "evidence_segment_ids": ["seg_0_12", "seg_0_13"]
    }
  ]
}
```

聚合代码只允许：

- 校验任务集合和返回集合完全一致；
- 校验枚举、ID、证据范围与字段长度；
- 合并字节级完全相同的问题；
- 合并同一问题重复列出的相同证据 ID；
- 以确定性顺序排序；
- 分配 `audit_issue_id`。

聚合代码不得根据语义相似度删除、改写或合并问题。

## 6. 跨卡统筹输出

```json
{
  "input_complete": true,
  "input_error": null,
  "audit_issue_decisions": [],
  "card_decisions": [],
  "revision_tasks": [],
  "search_tasks": [],
  "dropped_search_candidates": [],
  "todo_candidates": [],
  "dropped_todo_candidates": [],
  "unresolved_conflicts": []
}
```

### `audit_issue_decisions[]`

```json
{
  "audit_issue_ids": ["issue-0001"],
  "decision": "accepted",
  "reason": "逐字稿明确显示方案由另一位参与者提出。",
  "revision_task_key": "revision-1"
}
```

- `decision`：`accepted`、`combined` 或 `rejected`；
- `combined` 必须列出两个或更多确属同一问题的 `audit_issue_ids`；
- `rejected` 的 `revision_task_key` 必须为 `null`，并给出与证据或范围有关的理由；
- 每个输入 `audit_issue_id` 必须且只能出现一次。

### `card_decisions[]`

```json
{
  "decision_key": "decision-1",
  "operation": "revise",
  "source_card_ids": ["card-0001"],
  "target_scene_id": "work_communication",
  "reason": "卡片价值成立，但必须修正方案归属。",
  "revision_task_key": "revision-1"
}
```

- `operation`：`keep`、`revise`、`merge`、`drop` 或 `create`；
- `keep` 必须只有一个 `source_card_id`，且 `revision_task_key=null`；
- `revise` 必须只有一个 `source_card_id`；
- `merge` 必须有两个或更多 `source_card_ids`；
- `drop` 至少有一个 `source_card_id`，且 `revision_task_key=null`；
- `create` 的 `source_card_ids=[]`，必须由有效的 `missed_high_value_content` 问题支持；
- 除 `keep` 和 `drop` 外都必须引用且只引用一个 `revision_task_key`；
- 每张输入卡必须且只能被一个决定消费，不能同时保留和合并。
- `card_decisions` 数组顺序就是最终展示顺序；`drop` 项不占最终位置，后端按其余项目顺序连续生成 `position`。

### `revision_tasks[]`

```json
{
  "revision_task_key": "revision-1",
  "decision_key": "decision-1",
  "operation": "revise",
  "target_scene_id": "work_communication",
  "source_card_ids": ["card-0001"],
  "audit_issue_ids": ["issue-0001"],
  "requirements": [
    {
      "requirement_id": "req-0001",
      "instruction": "恢复方案提出者，并把用户状态改为尚未回应。",
      "evidence_segment_ids": ["seg_0_12", "seg_0_13"]
    }
  ],
  "preserve_points": ["保留现有方案权衡分析"],
  "remove_or_avoid": ["不要再把礼貌回应写成接受"],
  "completion_criteria": ["正文中的提出者和接受状态与逐字稿一致"],
  "search_task_keys": []
}
```

- 一项任务只生成一张最终卡；
- `requirement_id`、`revision_task_key`、`decision_key` 是本次输出临时键，必须在当前对象内唯一；
- 每个有效审核问题必须进入一个修改任务，或由 `drop` 决定完整解决；
- `preserve_points` 只能指向来源卡片中确实存在的强内容，不能新写结论；
- `search_task_keys` 只能引用本次输出的搜索任务。

### `search_tasks[]`

```json
{
  "search_task_key": "search-1",
  "source_candidate_ids": ["search-candidate-0001"],
  "question": "目标 API 当前正式文档支持哪些认证方式？",
  "purpose": "核验卡片中方案能否落地以及需要补充的接入条件。",
  "target_decision_keys": ["decision-1"],
  "related_segment_ids": ["seg_0_20"],
  "source_requirements": ["官方 API 文档"],
  "jurisdiction": null,
  "freshness_requirement": "以报告生成日仍有效的正式版本为准"
}
```

- `source_candidate_ids` 可以为空，但此时必须由有效审核问题或当前卡片中的明确外部信息缺口支持；
- 一个搜索任务必须至少影响一个需要修改的 `target_decision_key`；
- `question` 不能要求搜索服务判断录音内部事实或用户意图。

### `dropped_search_candidates[]`

```json
{
  "source_candidate_ids": ["search-candidate-0002"],
  "reason": "外部资料不会改变当前卡片的事实、判断或建议。"
}
```

每个输入搜索候选必须进入一个搜索任务或一条删除记录。

### `todo_candidates[]`

```json
{
  "source_todo_candidate_ids": ["todo-candidate-0001"],
  "text": "确认接口认证方式后更新接入方案",
  "owner_type": "user",
  "assignee_text": null,
  "due_at": null,
  "due_text": null,
  "evidence_segment_ids": ["seg_0_24"]
}
```

- `owner_type`：`user` 或 `other_requires_user_follow_up`；
- 每个保留待办必须至少引用一个输入待办候选和一个真实证据片段；
- 后端另外保存被删除候选及理由，模型不得无记录地遗漏候选。

### `dropped_todo_candidates[]`

```json
{
  "source_todo_candidate_ids": ["todo-candidate-0002"],
  "reason": "这是模型建议，不是用户已经接受或承诺的行动。"
}
```

每个输入待办候选必须进入一个全局待办或一条删除记录，且只能出现一次。

### `unresolved_conflicts[]`

```json
{
  "conflict_type": "todo_due_date",
  "related_ids": ["todo-candidate-0001", "todo-candidate-0002"],
  "description": "两个可靠片段给出不同截止日期，无法确认哪个更新。",
  "evidence_segment_ids": ["seg_0_24", "seg_1_10"]
}
```

存在任何未解决冲突时，不得开始最终发布。

## 7. 搜索资料包

每个 `search_task_key` 独立产生一个资料包：

```json
{
  "search_task_id": "search-task-0001",
  "status": "success",
  "answer": "官方文档列出了两种当前支持的认证方式。",
  "supported_points": [
    {
      "point": "支持 OAuth 2.0。",
      "source_ids": ["source-0001"]
    }
  ],
  "unresolved_points": [],
  "sources": [
    {
      "source_id": "source-0001",
      "title": "Authentication",
      "url": "https://example.com/official/authentication",
      "publisher": "Example",
      "published_at": null,
      "retrieved_at": "2026-09-02T12:00:00+08:00",
      "source_type": "official_documentation",
      "supports": [0]
    }
  ],
  "error_summary": null
}
```

- `status`：`success`、`partial` 或 `failed`；
- `supports` 引用 `supported_points` 的零基索引；
- `failed` 时 `answer=null`、`supported_points=[]`、`sources=[]`，并填写 `error_summary`；
- `partial` 只允许后续使用已有来源直接支持的 `supported_points`；
- 搜索适配器负责分配和校验 `search_task_id`、`source_id`、URL 与抓取时间。

## 8. 定向修改输出

```json
{
  "input_complete": true,
  "input_error": null,
  "reserved_card_id": "final-card-0001",
  "operation": "revise",
  "markdown": "# 修正后的完整卡片",
  "source_segment_ids": ["seg_0_12", "seg_0_13"],
  "used_source_ids": [],
  "todo_candidates": [],
  "completed_requirement_ids": ["req-0001"],
  "unresolved_requirement_ids": []
}
```

- `reserved_card_id` 和 `operation` 必须原样返回；
- `completed_requirement_ids` 与 `unresolved_requirement_ids` 的并集必须精确等于输入任务的全部 `requirement_id`，交集必须为空；
- 任一要求未解决时 `input_complete=false`；
- `source_segment_ids` 和 `used_source_ids` 必须经过后端存在性校验；
- `used_source_ids` 中的每个来源必须实际出现在 Markdown 对应外部事实附近；
- 修改输出不得携带新的搜索候选。需要追加搜索时必须回到统筹阶段产生新任务，不能在修改阶段私自执行。

## 9. 后端组装的最终卡片

模型不直接输出该对象。后端把 `keep` 卡片或定向修改结果与统筹决定组合成：

```json
{
  "card_id": "final-card-0001",
  "scene_id": "work_communication",
  "position": 0,
  "title": "方案归属仍需确认",
  "summary": "当前方案由另一位参与者提出，你尚未明确接受。",
  "markdown": "# 方案归属仍需确认\n\n完整卡片正文",
  "source_segment_ids": ["seg_0_12", "seg_0_13"],
  "used_source_ids": [],
  "origin_card_ids": ["card-0001"],
  "v1_markdown_sha256": "原卡片哈希；create 时为 null",
  "final_markdown_sha256": "最终卡片哈希",
  "untouched": false
}
```

- `title` 与 `summary` 由后端按已确认 Markdown 格式确定性提取，不增加模型调用；
- `position` 由统筹决定的稳定顺序生成；
- `untouched=true` 时只能来自一个 `keep` 卡片，且两个 Markdown 哈希必须相同；
- `origin_card_ids` 覆盖被修订或合并的来源卡；`create` 时为空数组。

## 10. 终审要求分配

终审计划由后端生成，不由模型自由决定：

- 有逐字稿证据的要求分配给覆盖对应 `evidence_segment_ids` 的 `evidence_chunk`；
- 跨卡重复、结构、主次和场景归属要求分配给 `global_report`；
- 同一要求涉及多个分段时可以分配给多个单元；
- 每项要求必须至少分配给一个单元；
- 每个单元只返回自己收到的 `requirement_id` 检查结果；
- 聚合后每项要求至少有一个 `completed=true`，并且不能存在任何同项 `completed=false`，否则终审失败。

## 11. 最终候选包与发布门槛

最终候选包由后端组装，不由模型直接生成：

```json
{
  "cards": [],
  "todo_candidates": [],
  "external_sources": [],
  "v1_card_hashes": {},
  "final_card_hashes": {},
  "untouched_card_ids": [],
  "completed_revision_task_ids": [],
  "search_degraded": false,
  "search_degraded_reason": null
}
```

只有同时满足以下条件才允许发布：

1. 七个场景结果均完成并通过 Schema 与 ID 校验；
2. 所有初审单元完成，代码聚合完整；
3. 跨卡统筹输入完整，没有未解决冲突；
4. 每个非 `keep` 决定均产生有效最终卡片或明确 `drop`；
5. 每个 `keep` 卡片的 `final_card_hashes` 与 `v1_card_hashes` 完全一致；
6. 搜索成功资料均经过来源校验，失败搜索已经降级；
7. 所有终审单元 `passed=true` 且问题为空；
8. 卡片、待办、证据和外部来源在同一数据库事务中发布。
