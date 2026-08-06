# Task 4 implementation report

## 状态

已完成四层 Prompt、事件地图、六场景严格 Schema、证据完整性校验和
默认 Prompt 安全升级。冻结规范中的 9 个 Markdown Prompt 资源均逐字落盘，
并已验证会进入 wheel。Task 5 的远程模型执行器、JSON 修复/重试和分析编排
未在本任务实现。

## 修改文件

- 新增固定 Prompt：`system.md`、`common-scene.md`、`event-map.md`。
- 新增事件与证据契约：`event_schema.py`、`evidence.py`。
- 重建六场景严格 Schema：`schemas.py`。
- 按冻结规范替换六份 `defaults/*.md`。
- 更新四层组装和不可信数据隔离：`composer.py`。
- 更新默认 Prompt 安全迁移：`store.py`。
- 新增/扩展事件、六场景、证据、组装、迁移和 API 测试。

## RED / GREEN 证据

1. 事件地图：
   - RED：首轮收集因缺少 `event_schema` 模块失败。
   - GREEN：先通过 3 项基础结构测试，补入说话人阈值、事件证据唯一性、
     assigned/unassigned 互斥、父事件存在/包含和无环校验后通过 7 项测试。
2. Todo / Meeting：
   - RED：严格结果类型尚不存在，测试以 import error 失败。
   - GREEN：实现 Todo 零卡片、Meeting 独立事件多卡片、事件绑定、标题和时间范围
     约束后通过 4 项首批场景测试。
3. Parenting：
   - RED：类型缺失；后续边界测试分别观察到低于 `0.60`、跨事件 finding、
     非 2–3 句建议话术和重复互动仍被接受。
   - GREEN：加入置信度、事件分组、finding/recommendation 引用、建议话术句数和
     单聚合卡约束后通过 7 项阶段测试。
4. Content：
   - RED：类型缺失；后续确认模糊来源、存在置信度不足却输出 creator、
     错误兴趣模式和合并不同事件内容仍可通过。
   - GREEN：加入来源/标题溯源、`inferred_title_hint`、低置信度 search-only、
     兴趣信号和独立 consumed item 约束后通过 11 项阶段测试。
   - 诊断边界 RED：`inferred_title_hint` 一度因全局 exclude 而无法序列化回读；
     GREEN：普通模型 dump 保留诊断字段，仅 `model_dump_for_frontend()` 删除它。
5. Growth：
   - RED：类型缺失；后续确认单事件低置信案例、无对方回应/负向结果、
     无单事件例外声明、未知 basis ID 和过多改进步骤仍可通过。
   - GREEN：加入 `0.80` 案例门槛、单事件例外声明、案例/方向交叉引用、
     最多 3 步及不确定资源 search-only 后通过 15 项阶段测试。
6. Inspiration：
   - RED：类型缺失；后续确认关键词堆砌、跨事件连接和带义务/截止日的灵感步骤
     仍可通过。
   - GREEN：加入事件内 idea 分组、连接引用、语义内容和非义务型 next step
     约束后通过 19 项阶段测试。
7. 六场景联合约束：
   - RED：缺少 discriminated union；随后针对卡片数量、通用标题、时间范围、
     话术句数和截止表达的测试均先出现实际 `DID NOT RAISE`。
   - GREEN：`StrictSceneResult` 形成六分支 `oneOf`；最终 JSON Schema 含
     6 个场景分支、40 个对象定义，所有对象均 `additionalProperties=false`。
8. 证据完整性：
   - RED：缺少 `evidence` 模块；首轮补齐后又有 5 个嵌套证据、3 个低身份置信度
     和 2 个绕过 Pydantic 构造的 basis 引用测试实际失败。
   - GREEN：实现事件/segment 存在性、跨事件、全量 transcript 归属、嵌套引用、
     身份阈值和二次防御性校验后，16 项证据测试通过。
9. 四层 Prompt 组装：
   - RED：缺少 `compose_event_map` / `compose_scene`；旧 runner 兼容测试又暴露
     `compose` 缺口；editable layer 注入测试曾观察到伪闭合标签计数为 2。
   - GREEN：实现固定顺序、JSON 数据包转义、可编辑 Prompt 转义、资源加载，
     并保留隔离的旧接口 adapter。
10. 默认 Prompt 升级：
    - RED：新增元数据、legacy hash 升级、归档和幂等测试出现 4 项真实失败。
    - GREEN：仅命中已知 legacy hash 时替换并归档；用户编辑保持逐字不变；
      重复初始化不再重复归档或递增版本，8 项 store 测试通过。
11. 回归兼容：
    - 首次完整 backend 运行是 `5 failed, 208 passed`，失败集中在旧
      `PromptComposer.compose(...)` 和旧 `SceneResult.model_validate(...)`。
    - 将两者收敛为明确标注、可由 Task 5 删除的 compatibility adapter 后，
      旧聚焦回归先通过 5 项，随后完整 backend 通过。

## 契约与阈值矩阵

| 契约 | 强制约束 |
| --- | --- |
| EventMap | event ID 唯一；segment 不重复；assigned 与 unassigned 互斥；父事件存在、无环且时间包含子事件 |
| UserSpeaker | `confidence >= 0.70` 且 speaker 非空时才可靠 |
| Todo | 顶层无卡片；只允许 user/shared todo；媒体行为号召不能直接成为用户 todo |
| Meeting | 可按独立事件生成多卡；每卡只绑定一个 event；detail 与 card event 一致；决定必须 confirmed |
| Parenting | 最多一张聚合卡；interaction 仍按 event 分组；issue `confidence >= 0.60`；recommendation 只引用本 interaction finding；话术 2–3 句 |
| Content | 最多一张聚合卡；每个 consumed item 独立且绑定 event；存在性 `<0.90` 时只给 search query；兴趣模式与证据数量严格匹配 |
| Growth | 最多一张聚合卡；case `confidence >= 0.80`；单事件必须有对方反应、负向结果和“单事件例外”声明；basis 只引用本结果 ID；最多 3 步 |
| Inspiration | 最多一张聚合卡；idea 按 event 分组；connection 只引用已知 idea；拒绝纯关键词和义务/截止式 next step |
| Evidence | 所有 segment 必须归属 event 或 unassigned；引用必须存在并位于对应 event；低于身份阈值时禁止用户归因型产物，但允许客观内容记录 |

所有 Pydantic 对象均使用 `extra="forbid"`。六场景顶层统一执行：
`generation_state=false` 时 cards/todos 必须为空；为 true 时必须满足各场景要求，
且拒绝冻结规范列出的通用标题。

## Prompt 四层顺序与不可信数据边界

组装顺序固定为：

1. system / security 固定规则；
2. event-map 或 common-scene 固定规则；
3. 用户可编辑的场景 Prompt；
4. 目标 JSON Schema。

transcript、event map 和 profile 均先 JSON 序列化，再放入明确的
`<untrusted_*_data>` 数据包；其中 `<`、`>`、`&` 会转义，不能闭合容器或伪造新
指令层。用户可编辑 Prompt 也会转义层标签。固定规则从包资源读取，调用方不能
通过 editable Prompt 改写前两层。

为避免 Task 4 破坏现有 backend，`composer.py` 和 `schemas.py` 底部保留了独立、
明确标注为 Task 5 可删除的 legacy adapter；新路径只使用 `compose_event_map`、
`compose_scene` 和 `StrictSceneResult`，严格契约未被旧模型放宽。

## 默认 Prompt 安全升级

- 新安装写入 packaged V2，metadata 标记 `packaged_default_version=2` 和
  `current_source=packaged`。
- 仅当现有 `current.md` 的 SHA-256 精确命中每个场景已知 legacy 默认值时，
  才将旧内容确定性归档到 `versions/`，替换为 V2 并递增版本。
- 每个场景覆盖旧文件原始字节和旧 store `.strip()` 后字节两种 hash；六场景
  已逐个用 Git 中的旧默认内容验证。
- 已是 V2 时不归档、不增版本；任意未命中 hash 的用户内容逐字保留，metadata
  标记为 user。重复初始化幂等。
- API 仍固定为六场景，未新增 add/delete 能力。

## 冻结资源与打包验证

- `system.md`、`common-scene.md`、`event-map.md` 和六份场景默认 Prompt，
  共 9 个资源均与冻结规范对应 fenced block 逐字相同。
- 构建 wheel 成功，归档清单确认上述 9 个 Markdown 文件全部包含在发行包。
- `StrictSceneResult` JSON Schema 为 6 个 `oneOf` 分支；40 个对象定义全部禁止
 额外字段。

## Fix round 1 审查闭环

### RED / GREEN 证据

1. 低置信具体作品名：
   - RED：Growth `LearningResource` 在 `existence_confidence=0.89` 时仍接受
     具体 `title`，且 `title=None` 无法表达 search-only 资源。
   - GREEN：Content recommendation 与 Growth resource 统一为：低于 `0.90`
     时 `title` / `creator` 必须均为 null，`0.90` 边界开始允许具体作品。
2. 分层 confidence 门槛：
   - RED：6 种 `should_generate=true` 结果和 5 种可见卡片均接受
     `0.29`；待办责任归属、Growth 用户评价和画像兴趣信号均接受
     `0.49`。
   - GREEN：逐项验证 `0.29/0.30` 和 `0.49/0.50` 边界，未使用全局
     单一门槛。
3. 前端递归 allowlist：
   - 六场景均递归收集 key，确认不含内部 confidence、event/evidence ID、
     finding/case/basis ID、`generation_reason`、`internal_interest_signals` 和
     `inferred_title_hint`。
   - 测试使用非空 `meeting_todos`、Content `recommendations`、Growth
     `cases` / `resources`，并确认标题、核心内容、详情、时间、可展示待办仍保留。

### confidence 阈值矩阵

| 对象 | 最低 confidence | 理由 |
| --- | ---: | --- |
| `should_generate=true` 场景 | 0.30 | 低于 0.30 不得输出结论 |
| Meeting / Parenting / Content / Growth / Inspiration 可见卡片 | 0.30 | 可见结论最低门槛 |
| Todo 用户/共同责任归属 | 0.50 | 0.30–0.49 不得用于待办归属 |
| Growth case 中的用户评价 | 0.50 | 个人评价门槛；单事件另受 0.80 更严门槛 |
| `internal_interest_signals` 画像候选 | 0.50 | 画像更新不接受薄弱可能性 |
| Parenting issue | 0.60 | 保留冻结规格门槛 |
| User identity | 0.70 | 保留事件地图身份门槛 |
| Growth 单事件 case | 0.80 | 保留单事件例外门槛 |
| 具体外部作品 | existence confidence 0.90 | 低于门槛只能 search-only |

### 前端 allowlist 矩阵

| 场景 | 保留的主要嵌套内容 | 专项覆盖 |
| --- | --- | --- |
| Todo | 可展示待办字段 | 待办证据/事件 ID 被清理 |
| Meeting | 参与人、结论、决定、问题、`meeting_todos` | 嵌套待办可见且内部 ID 被清理 |
| Parenting | 分事件互动、发现和建议 | finding / basis / evidence ID 被清理 |
| Content | 消费内容、跨事件洞察、`recommendations` | 保留 search query，清理兴趣信号与推断标题 |
| Growth | 方向、`cases`、建议、`resources`、优势 | case / direction / basis / event ID 被清理 |
| Inspiration | 灵感、连接、探索步骤 | event / evidence ID 和 confidence 被清理 |

## 最终验证

- 首版 Task 4 指定 Prompt + API 套件：`81 passed in 0.29s`。
- 首版完整 backend 回归：`234 passed in 3.35s`。
- Fix round 1 Task 4 指定 Prompt + API 套件：`132 passed in 0.33s`。
- Fix round 1 完整 backend 回归：`285 passed in 3.65s`。
- 冻结 Prompt 与规格 fenced block 精确比对：`9/9`。
- wheel 构建成功，9 份 Prompt Markdown 资源全部在包内：`9/9`。
- `compileall` 和 `git diff --check`：通过。
- JSON Schema 复核：`oneOf=6`、`defs=40`、所有对象严格，且
  `inferred_title_hint` 为必填内部诊断字段。
- `git diff --check`：通过。

## 风险与自审

- 冻结 event-map Prompt 给出了字段语义但没有逐个规定 Python 字段名；实现使用
  `speaker_ids`、`user_role`、`user_role_confidence`、`local_date`、`timezone`
  承载这些语义。后续模型适配器必须按生成的 JSON Schema 输出，而非猜测字段名。
- Growth 的“高影响”没有独立冻结字段；当前用 `confidence >= 0.80`、真实对方回应、
  负向结果和顶层“单事件例外”理由共同约束。是否属于高影响仍需模型依据固定
  Prompt 判断。
- 媒体类 event 上的 user todo 被保守拒绝。若用户在媒体播放期间明确口头承诺
  follow-up，event mapper 应把该段拆成 conversation/commitment event，否则会产生
  可接受的 false negative，而不是把媒体号召误判为用户意图。
- `inferred_title_hint` 有意保留在内部诊断序列化中，只在
  `model_dump_for_frontend()` 删除；Task 5/发布边界必须调用该方法，不能把内部提示
  直接暴露给前端。
- legacy `compose` / `SceneResult` adapter 只为现有 runner 回归兼容；Task 5 切换到
  严格结果后应删除或完全停用这条旧路径。
- 自审未发现 Task 5 runner、Task 6 publisher、数据库迁移、转写或音频处理变更。

## 提交

- 提交消息：`feat: freeze evidence-backed scene prompts`。
- 提交哈希见承载本报告的 Git 提交；具体短哈希在任务交付消息中记录。
