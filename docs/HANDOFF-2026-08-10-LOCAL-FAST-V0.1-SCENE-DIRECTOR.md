# Local Fast V0.1 场景导演阶段交接文档

**交接日期：** 2026-08-10  
**目标分支：** `codex/local-fast-v0-1`  
**worktree：** `/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1`  
**交接前 HEAD：** `8ed1b8b203f5a20ce42ab6a132be680a8dc8cf38`  
**当前阶段：** DeepSeek 基础修复已完成；准备实现共享场景导演、服务端上下文扩展和会议 Prompt 增强

## 1. 新窗口目标

继续完成 Local Fast V0.1，按以下顺序推进：

1. 用户审阅并确认共享场景导演规格；
2. 根据确认后的规格编写详细实施计划；
3. 实现共享场景导演、全文簇、服务端上下文扩展和证据范围改造；
4. 第一轮只增强会议 Prompt；
5. 复用现有 3,442 个可靠句段重新分析，不重新转写；
6. 自动化验证通过后，直接打开历史页面让用户验收会议质量；
7. 会议通过后再接入联网核验能力；
8. 逐个增强其余五场景；
9. 最后实施 Compact 本地转写加速。

不要在场景导演和会议效果验收前开始 Compact，也不要新增日报或新卡片类型。

## 2. 新窗口必须先读

按顺序完整阅读：

1. `docs/LOCAL-FAST-V0.1-PARAMETER-BASELINE.md`
2. `docs/superpowers/specs/2026-08-10-local-fast-v0-1-design.md`
3. `docs/superpowers/specs/2026-08-10-historical-quality-analysis-design.md`
4. `docs/superpowers/specs/2026-08-10-scene-director-meeting-context-design.md`
5. `docs/benchmark-evidence/2026-08-10-local-fast-v0-1-deepseek.md`
6. `docs/benchmark-evidence/2026-08-10-local-fast-v0-acceptance.md`
7. 本交接文档

共享场景导演的完整 Prompt 已写在第 4 份规格的“附录 A”，它是实施基线，不是示例。

## 3. 用户已经确认的产品方向

用户希望至少恢复此前人工/智能体分析的历史质量。历史高质量链路的关键能力是：

- 按时间簇保留完整转写；
- 人工或智能体选择高价值工作、面试、职业、亲子和内容场景；
- 对入选场景回读完整上下文及相邻句段；
- 六类信息再做场景分析和综合；
- 可联网核验节目、人物和外部知识。

本轮已确认：

- `unassigned` 不应决定句段能否进入分析；
- 转写层应保留更多文字和更精准的句级时间戳；
- 服务端只负责结构有效、时间正确和证据可回听；
- 场景和价值判断交给大模型；
- 当前先不加日报，先丰富六个现有场景；
- 六场景顺序为逐个补充，第一项是会议；
- 先做共享场景导演和服务端上下文扩展，再做联网核验，Compact 最后。

## 4. 当前产品结果与质量差距

### 4.1 已完成的 DeepSeek 基础修复

长录音分析已从一次巨型 Event Map 改为确定性局部窗口：

- 文件变化切开；
- 相邻句段间隔不小于 45 秒切开；
- 单窗口最多 20 分钟；
- 单窗口最多 400 个句段。

真实重跑结果：

- 可靠句段：3,442；
- Event Map 调用：23；
- 全局事件：24；
- Event 已分配句段：1,314；
- `unassigned` 句段：2,128；
- 当前分析版本：`0029970e-eb50-49a7-b683-3b53b7e931a7`；
- 页面可见结果：3 张（2 张面试/工作沟通会议卡、1 张灵感卡）；
- 全局待办：0。

此前“completed 但 0 卡片”的版本：`fa0c5b48-b2c6-445b-ae91-0b78d5ffc7f6`。它只有一个巨大的 `casual_chat` 事件，用户身份未知，六场景全部不生成。

### 4.2 为什么仍未恢复历史质量

当前六场景只读取 Event Map 已分配的 1,314 个句段，2,128 个通过可靠门槛的句段被挡在场景模型之外。Event Map 同时承担“粗索引”和“内容准入闸门”，前置漏判会成为永久信息损失。

历史链路会对高价值时间范围读取全部句段，因此能产生更完整的工作、招聘、职业、亲子和内容分析。当前缺失的主要不是转写，而是“共享筛选—完整回读—场景分析”这一层。

## 5. Event Map 的新定位

Event Map 暂不删除，但降级为可选兼容索引：

- 提供粗粒度时间目录、标题摘要和身份线索；
- 提供现有卡片、待办、发布和回听所需的稳定 `event_id`；
- 不再决定句段是否有资格进入导演或六场景；
- Event Map 为空或漏判时，导演仍可从全部可靠文字发现场景；
- 真正漏掉的场景由服务端建立稳定补充事件锚点。

会议验收后再评估是否彻底删除 Event Map。彻底删除需要把稳定 ID、身份判断、跨场景关联、历史兼容和发布锚点迁移到场景档案，不属于当前最小恢复路径。

## 6. 已批准的目标链路

```text
全部结构有效的可靠句段（当前 3,442 个）
  → 确定性全文簇
  → 共享场景导演
  → 服务端校验选择并扩展前后上下文
  → 场景档案 SceneDossier
  → 六场景按各自档案读取完整原文
  → 按档案范围校验证据
  → 现有卡片、待办和回听发布链路
```

### 6.1 全文簇

- 所有可靠句段进入，不受 Event Map 分配影响；
- 沿用文件边界、45 秒间隔、20 分钟和 400 句段参数；
- 每条句段保留 `segment_id/file_id/start_ms/end_ms/speaker_id/text`；
- cluster ID 对同一转写快照稳定；
- 不重新转写、不改写正文。

### 6.2 共享场景导演

导演读取完整文本簇和仅供参考的 Event hints，输出高价值选择：

- `selection_id`
- `cluster_ids`
- `source_event_ids`
- `candidate_scenes`
- `title`
- `selection_reason`
- `value_signals`
- `priority`
- `context_before_clusters`
- `context_after_clusters`

导演只负责选场景和划范围，不生成最终卡片、待办、建议或成长评价。

完整 Prompt 位于：

`docs/superpowers/specs/2026-08-10-scene-director-meeting-context-design.md` 的附录 A。

### 6.3 场景档案

`SceneDossier` 至少包含：

- `dossier_id`
- `primary_event_id`
- `source_event_ids[]`
- `candidate_scenes[]`
- `selected_cluster_ids[]`
- `expanded_cluster_ids[]`
- `allowed_segment_ids[]`
- 文件与时间范围
- 标题、选择原因和优先级

六场景证据必须属于档案 `allowed_segment_ids`，不再要求属于 Event 原始 `evidence_segment_ids`。

### 6.4 上下文扩展边界

- 只能使用同一文件的直接相邻簇；
- 每侧最多一个簇；
- 扩展后最多 30 分钟；
- 扩展后最多 600 句段；
- 超限时保留核心簇，裁掉最远相邻上下文；
- 未知 ID、跨文件和时间越界必须失败。

### 6.5 `unassigned` 兼容策略

- 新 DeepSeek Event Map 响应使用不含 `unassigned_segment_ids` 的 `EventMapDraft`；
- 服务端规范化为持久化 `EventMap` 时计算兼容字段；
- 兼容字段只用于读取旧版本和内部覆盖统计；
- 不发送给导演或六场景；
- 不参与内容准入和证据准入。

## 7. 会议 Prompt 第一轮目标

会议扩展为“值得回顾的工作沟通”，包括：

- 正式会议；
- 招聘和面试；
- 职业发展讨论；
- 组织调整沟通；
- 产品、业务、技术和项目讨论；
- 负责人电话；
- 有明确问题、观点、分歧、结论或行动的非正式工作交流。

会议结果应尽量利用现有 Schema 填充：

- 场景背景和参与角色；
- 讨论主题和各方立场；
- 已确认事实；
- 明确结论与决策；
- 开放问题、分歧和依赖；
- 明确行动与待确认行动；
- 对后续最有价值的回顾摘要。

第一轮不改 `MeetingSceneResult` Schema、不新增卡片类型。身份未知时使用“候选人、面试官、参与者”等角色，不使用“你”；不可靠行动只留在会议卡中，owner 为 `unknown`，不得提升为全局待办。

## 8. 尚未实施的内容

以下内容仅有规格，没有生产代码：

- `EventMapDraft` 新模型契约；
- 全文簇构建器；
- 共享场景导演 Prompt、Schema 和调用；
- 导演批次选择合并；
- `SceneDossier`；
- 服务端相邻上下文扩展；
- 按档案范围的证据校验；
- 漏事件补充锚点；
- 会议 Prompt V0.1 增强；
- 本轮真实 3,442 句段重跑和页面验收。

不要把“设计已提交”误判为“功能已实现”。

## 9. 新窗口第一步

当前处于书面规格用户审阅关口。新窗口应先询问用户是否确认：

`docs/superpowers/specs/2026-08-10-scene-director-meeting-context-design.md`

若用户确认，立即使用 writing-plans 流程编写：

`docs/superpowers/plans/2026-08-10-scene-director-meeting-context.md`

实施计划必须先锁定具体文件、函数签名、Schema、测试和提交边界，再开始改代码。不得跳过实施计划。

如果用户要求修改共享场景导演 Prompt，先修改规格附录 A、完成自检并提交，再写计划。

## 10. 预计代码边界

规格中的预计范围：

- `backend/src/audio_memory/analysis/clusters.py`
- `backend/src/audio_memory/analysis/director.py`
- `backend/src/audio_memory/analysis/dossiers.py`
- `backend/src/audio_memory/analysis/runner.py`
- `backend/src/audio_memory/reanalysis/worker.py`
- `backend/src/audio_memory/prompts/director_schema.py`
- `backend/src/audio_memory/prompts/composer.py`
- `backend/src/audio_memory/prompts/evidence.py`
- `backend/src/audio_memory/prompts/event_schema.py`
- `backend/src/audio_memory/prompts/director.md`
- `backend/src/audio_memory/prompts/common-scene.md`
- `backend/src/audio_memory/prompts/defaults/meeting.md`
- 对应 unit、integration、prompt-eval 和 E2E tests

这只是规格边界。实施计划必须重新阅读现有代码并给出准确接口，不要机械创建全部文件，也不要做无关重构。

## 11. 自动化验收重点

至少覆盖：

1. Event 未引用的可靠句段仍进入全文簇和导演请求；
2. 新模型契约不要求 DeepSeek 输出 `unassigned_segment_ids`；
3. cluster ID 稳定；
4. 导演拒绝未知簇、未知事件和非法场景；
5. 同一档案可以进入会议和待办；
6. Event Map 完全漏掉的场景能建立稳定锚点；
7. 原 Event 未分配但属于档案的句段可以作为合法证据；
8. 档案外、未知、跨文件和时间越界证据必须失败；
9. 媒体访谈不能误判为现场会议；
10. 没有明确决策时不得虚构决策；
11. 身份未知时不得强归因或生成用户待办；
12. 原子发布、历史版本、卡片详情和回听不回退。

完成后运行后端全量测试、前端单测、生产构建和关键 Playwright 流程。

## 12. 真实数据验收

复用 source job：`d29475e4-f148-4b99-9b7e-1e5751da1e48`。

不得执行 VAD、Whisper、风险门、说话人识别或 Compact。只创建新的 analysis version，保留旧版本。

页面验收至少检查：

- 导演确实读取全部 3,442 个可靠句段；
- `unassigned` 不再是内容或证据准入条件；
- 能识别招聘、职业、组织变化和产品/业务深度交流；
- 会议卡不只是短摘要，包含完整讨论脉络、结论、开放问题和行动；
- 卡片证据能够回听；
- 不虚构现场会议、正式决策、负责人、人名、亲子或驾驶事实；
- 身份未知时不生成强归因用户待办；
- 用户通过历史页面直接验收。

会议质量通过后才进入联网核验设计和实现。

## 13. 当前服务状态

交接时端口 `127.0.0.1:8765` 显示 Python 进程 PID `23747` 监听，进程工作目录是正确的：

`/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1/backend`

但同一时刻 HTTP 探测 `/history` 返回无法连接。新窗口不得假设服务健康；展示前先重新检查监听、服务日志和 HTTP 状态，必要时安全停止僵死进程并从本 worktree 启动。不要从原始 workspace 启动后误验收旧代码。

默认产品地址：`http://127.0.0.1:8765/history`。

## 14. Git 与未提交文件

当前分支：`codex/local-fast-v0-1`。

关键提交：

- `8ed1b8b docs: specify scene director prompt`
- `c8600b2 docs: design scene director meeting context`
- `9138e1b docs: record historical-quality deepseek run`
- `00fe9a2 fix: derive event bounds from evidence`
- `f88430b fix: reject empty long-audio analysis`
- `496e41f feat: recover objective work communication cards`
- `f486304 feat: analyze transcript windows before scene synthesis`
- `dd2f92d feat: merge local event maps with conservative identity`
- `61e2170 feat: split long analysis into evidence windows`

交接时存在以下未跟踪内容，均不要清理、覆盖或误提交：

- `backend/.venv`
- `prototype/node_modules`
- `docs/prompt-editing/2026-08-10-deepseek-current-prompts.md`

其中 Prompt 编辑副本是此前为了用户审阅导出的当前 Prompt，尚未提交。除非用户明确要求，不要把它与功能提交混在一起。

不要使用 `git reset --hard`、`git checkout --` 或递归删除。每个实施任务采用测试先行和小步提交。

## 15. 隐私边界

允许记录：commit、branch、模型 ID、audio SHA-256、时长、大小、聚合数量、簇数、档案数、阶段耗时、token、调用次数、错误码、请求响应字节数和覆盖计数。

禁止记录：原音频路径、转写正文、音频片段、API Key、完整模型输入输出，以及包含个人内容的页面截图。真实转写不得进入仓库 fixture。

## 16. 后续路线

### A. 当前子项目

共享场景导演 + 服务端上下文扩展 + 证据范围改造 + 会议 Prompt + 页面验收。

### B. 联网核验

当前子项目验收后单独设计：搜索触发条件、查询生成、来源可信度、引用和缓存。内容场景首先使用；不得把外部搜索结果伪装成音频证据。

### C. 其余五场景

按用户逐个验收的方式增强：待办、亲子、内容、成长、灵感。具体顺序可在会议验收后再次确认。

### D. Compact

最后实施本地 Compact 转写加速，沿用参数基准和既有 Compact 设计。目标仍是同一 3 小时 31 分钟音频本地转写 35–45 分钟，二次 Whisper 调用为 0，并通过质量、时间戳和回听门槛。

## 17. 新窗口启动提示词

> 请按照交接文档继续落地 Local Fast V0.1：`/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1/docs/HANDOFF-2026-08-10-LOCAL-FAST-V0.1-SCENE-DIRECTOR.md`。先完整阅读其中列出的参数基准、历史质量设计和场景导演规格。当前 DeepSeek 基础修复已经完成，页面已有 3 张结果，但六场景只看到了 1,314/3,442 个可靠句段。请先确认书面规格；确认后先写实施计划，再实现共享场景导演、服务端上下文扩展、证据范围改造和会议 Prompt。使用现有 3,442 个可靠句段重跑，不重新转写；完成后直接打开历史页面让我验收。联网核验随后做，Compact 最后做。

## 18. 当前完成定义

当前子项目只有同时满足以下条件才算完成：

1. 全部 3,442 个可靠句段可被导演读取；
2. `unassigned` 不再控制分析和证据准入；
3. 场景档案提供完整且有界的前后上下文；
4. 会议卡恢复到接近历史报告的事实召回和内容丰富度；
5. 身份和承诺归因仍然保守；
6. 证据可回听且不能引用档案外句段；
7. 自动化测试、构建和关键浏览器流程通过；
8. 真实数据只重跑分析，不重跑转写；
9. 用户在页面确认会议效果可接受；
10. 未提前实施联网核验、其他场景或 Compact。
