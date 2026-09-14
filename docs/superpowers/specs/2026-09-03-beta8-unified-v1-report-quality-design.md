# Beta 8 统一 V1 生成与报告质量改造设计

**日期：** 2026-09-03
**状态：** 报告质量规则、同一正式方案内的两次 V1 调用架构和 Task 13 指定的非付费自动化回归已落地；最终整分支独立复审为 PASS（0 Critical / 0 Important）。前三次 V4 Pro 付费尝试分别暴露了索引截断、旧 CLI 未留存精确异常、以及 V1 待办字段嵌套层级错误；对应失败收口、哈希绑定恢复和确定性结构归位均已按 TDD 落地。为降低后续试错费用，已新增与正式验收隔离的 `mechanical-smoke` 层级：它只允许 `deepseek-v4-flash`，产物明确记录 `qualifies_as_final_acceptance=false`。第四次 Flash 机械链路运行在 `event_index` 输出恰好达到 32,000 Token 后截断；对比已成功的 Pro 索引只有 52 单元、52 范围、约 13,365 JSON 字符和 6,400 Token，确认 32k 不是该索引的客观必要长度；当前验收不再使用 Flash 索引，64k 仅作未来故障缓冲。第五次复用 Pro 索引、只新增一次 Pro `all_scenes_v1` 调用：259,449 输入、42,583 输出，未截断，但被隐私投影中未剔除合法 `omitted_units` 的确定性误判拦截。该门禁矛盾已按 TDD 修正，原始 V1 仍保留完整省略账本；下一次 Pro V1 付费恢复、人工质量验收与发布仍待新的明确授权
**架构决策：** 正常 V1 路径固定为两次调用：第一次只产生不做价值筛选的全量事实索引，第二次同时读取完整逐字稿与索引，产生可直接展示的七场景完整 V1。旧 X 方案中“第一次调用预先筛选有独立价值候选”的做法被废弃。
**实施目标：** 在不改变当前 Kimi 2.6 搜索候选、执行数量与采用决策逻辑的前提下，用“全量事实索引＋统一完整 V1 写作”保留 Y 的覆盖完整、核心判断和标题层级，保留 X 的单卡丰富度，并解决 X 的内容遗漏和结构不稳定、Y 的内容单薄，以及工作沟通过度拆分、跨场景归属错误、低价值出卡、结构混乱、建议空泛、报告腔、Markdown 重复编号与搜索来源错配。

## 1. 背景与问题定义

首次真实长录音评测产出 23 张卡，其中工作沟通 14 张。用户评审确认了以下系统性问题：

1. 同一次工作沟通被按议题拆成多张卡，破坏了回顾单位。
2. 卡片大量使用连续长段落，核心信息、事实、分析和建议混在一起。
3. 模型在标题中手工编号，前端又自动编号，出现 `1 一、`。
4. Kimi 完成了 10 个搜索任务，但搜索采用效果弱；最终来源表还发生了跨任务 ID 碰撞。
5. 建议停留在原则层，缺少可直接使用的表格、步骤、话术、清单或验证方法。
6. 第三方案例被错误绑定到用户本人，并被归入自我成长。
7. 可直接计算或没有独立帮助价值的信息也被制作成卡。
8. 正文使用“这份记录围绕……”“你从一段记录中看到……”等第三方报告腔，先复述材料，后给结论。
9. 七个场景分别读取同一份完整逐字稿，造成重复输入，也让不同场景在相互不知情的情况下重复出卡。

这些问题不能通过修改几句文案解决，需要调整 V1 生成边界、工作沟通组织规则、主体归属、价值准入、写作契约、审核契约和前端渲染契约。

## 2. 改造目标

### 2.1 两次调用生成完整 V1

正常情况下，V1 阶段固定为两次 DeepSeek 调用。第一次只建立全量事实索引，不决定出卡、场景取舍或呈现结构；第二次读取同一份完整逐字稿和索引，同时返回七个场景的完整 V1 结果。最终 V1 是可以直接展示的完整报告，不是大纲、摘要或待后续扩写的草稿。

### 2.2 工作沟通以真实沟通次数为单位

一次会议、拜访、电话、线上沟通、一对一、面试、客户或供应商交流，各对应一张“工作沟通”卡。同一次沟通中的多个议题放入卡内章节，不按议题拆卡。

### 2.3 非工作沟通卡仅依据价值出卡

不设卡片数量上限，不使用“每日最多 N 张”代替判断。没有独立理解、决策、记忆、风险或行动帮助的内容不出卡；一旦出卡，必须充分展开其价值。

### 2.4 卡片内容必须丰富

“丰富”指高信息密度和充分帮助，不是为达到长度而重复。根据卡片实际内容，完整呈现关键事实、核心判断、证据、原因、分歧、权衡、影响、已确定项、未确定项、行动和可直接使用的帮助。

### 2.5 表达直接且结构匹配内容

先呈现核心判断或核心信息组，再给事实和论据，最后展开影响与帮助。所有场景都必须根据内容使用小标题、短段落、表格、列表、时间线、清单、步骤或可复制话术，不允许复杂信息堆成一整段。

### 2.6 归属依据完整语义和表达用途

是否能用于分析用户，取决于谁在表达、表达对象是谁、以什么身份表达、为什么表达、用户是否将内容联系到自己。不依据“会议”或“朋友聊天”这类场景标签一刀切，也不屏蔽“离职”“疲惫”“犬儒”等词。

## 3. 非目标

本轮不做以下修改：

1. 不改变搜索候选的产生逻辑。
2. 不改变搜索任务数量的决策逻辑。
3. 不改变 Kimi 2.6 搜索调用方式和结果采用逻辑。
4. 不为非工作沟通卡设置数量上限。
5. 不要求每张卡使用相同栏目或必须使用表格。
6. 不为某个人名、单个错误案例或固定关键词写特判规则。
7. 不改造转写、可靠性门或证据回听链路。
8. 不改变 `SingleReportRunner` 默认链路和历史 Beta 8 产物的读取能力。

本轮虽不改搜索产生和采用逻辑，但必须修复来源 ID 碰撞，因为这是数据正确性缺陷，不是搜索策略变更。“搜索 10 个、最终只采用 2 个来源”的体验问题本轮不解决。

## 4. 目标链路

```text
可靠逐字稿
    ↓
第一次：全量事实索引
    ├─ 覆盖每份录音的所有实质内容单元
    ├─ 标记每次工作沟通的边界和主体
    ├─ 标记媒体播放、第三方案例、本人经历与模型内容
    └─ 不做价值筛选，不生成卡片或建议
    ↓
全量覆盖门禁
    ├─ 所有输入文件都已声明覆盖状态
    ├─ 所有大段未采用区间都有噪声、重复或无法解析理由
    └─ 工作沟通和媒体内容类型均有闭包检查
    ↓
第二次：“全场景完整 V1”模型调用
    ├─ 同时读取完整逐字稿和全量索引
    ├─ 识别每一次独立工作沟通
    ├─ 每次工作沟通生成一张完整卡
    ├─ 判断其他六个场景的独立价值
    ├─ 完成表达主体和对象绑定
    └─ 在生成前完成初步跨场景去重
    ↓
逐字稿分片初审
    ↓
全局编辑审核
    ├─ 处理初审问题
    ├─ 复核工作沟通边界
    ├─ 复核主体、场景、价值与重复
    └─ 规划搜索与定向修订
    ↓
Kimi 2.6 搜索
    ↓
受影响卡片定向修订
    ↓
最终审核
    ↓
确定性清理与原子发布
```

## 5. 两次全场景 V1 生成

### 5.1 第一次：全量事实索引

第一次调用读取全部可靠逐字稿，按时间和语义边界登记所有实质内容单元。索引仅包含路由和溯源字段：单元 ID、所在文件、内容类型、起止片段、主体、表达用途和一句中性描述。

第一次调用严禁：

- 判断哪些内容值得出卡；
- 按七场景删除或筛选候选；
- 生成核心洞察、建议、待办、搜索任务或 Markdown；
- 生成详细证据摘录、卡片大纲或写作计划。

这个边界专门解决旧 X 方案将“独立价值”前置，从而漏掉播客等内容的问题。

### 5.2 第二次：完整 V1 写作

第二次调用必须同时获得：

1. 与第一次字节级一致的完整可靠逐字稿；
2. 已通过覆盖门禁的全量事实索引；
3. 完整的共享质量规则和七场景能力，不使用实验中的压缩摘要版。

索引是防漏导航，不是输入边界。任何时候索引与逐字稿冲突，均以逐字稿为准，并在内部更正记录中保留可追溯原因。

### 5.3 完整 V1 输出

运行时使用薄 JSON 保存路由、证据、审核和恢复信息，用完整 Markdown 保存用户可见内容：

```json
{
  "input_complete": true,
  "input_error": null,
  "scene_results": [
    {
      "scene_id": "work_communication",
      "cards": [
        {
          "draft_card_key": "card-work-1",
          "card_basis": {
            "type": "work_communication",
            "source_unit_ids": ["communication-1"],
            "communication_kind": "meeting"
          },
          "markdown": "# 具体标题\n\n核心信息区……",
          "source_segment_ids": ["seg_0_12"],
          "search_candidates": []
        }
      ],
      "todo_candidates": [],
      "skip_reason": null
    }
  ],
  "omitted_units": [
    {
      "unit_id": "other-3",
      "reason": "只有可直接计算的日期确认，没有独立理解、决策、记忆、风险或行动帮助"
    }
  ]
}
```

非工作沟通卡的 `card_basis.type` 为 `independent_value`，`source_unit_ids` 引用其来源索引单元，`communication_kind` 为 `null`。`omitted_units` 只记录第二次调用判断为不成卡的非工作沟通索引单元，用于防止静默遗漏；`kind=work_communication` 已表示具有可识别工作目的和实质内容，因此不得进入 `omitted_units`。这些字段仅用于后端校验，不展示给用户。

### 5.4 Schema 硬约束

1. `scene_results` 必须恰好包含七个场景，不得缺失或重复。
2. 场景顺序固定，场景允许零张卡。
3. 有卡时 `skip_reason=null`；无卡时必须给出跳过原因。
4. 所有 `segment_id` 必须来自输入。
5. 工作沟通卡必须恰好引用一个 `kind=work_communication` 的索引单元；一个跨文件沟通单元可以包含多个片段范围。该 kind 的每个单元必须恰好对应一张工作沟通卡，不得省略、拆分或与另一次工作沟通合并。
6. 每个非工作沟通索引单元必须至少被一张卡的 `source_unit_ids` 引用，或恰好出现在 `omitted_units` 中。
7. 模型只生成本次输出内的 `draft_card_key`，不生成最终稳定卡片 ID；后端校验后分配。
8. Markdown 必须通过标题、核心信息区、章节结构和冗余编号检查。

后端在 V1 物化时从工作卡的 `source_unit_ids` 建立模型外的内部薄映射，由统筹校验、keep/revise 物化、最终 bundle 与发布前闭包校验持续保留。该映射的字段名和值均不进入初审、终审、统筹或修订模型载荷，也不进入用户可见 Markdown。发布前必须由发布器从与逐字稿和固定规则哈希绑定的事件索引 checkpoint 独立派生预期集，并验证每个预期工作沟通单元恰好出现在一张非 drop 的 `work_communication` 最终卡中。

### 5.5 失败恢复

1. 正常成功路径固定调用两次：全量索引一次，完整 V1 写作一次。
2. 索引未通过覆盖门禁时不得启动 V1 写作；仅能对索引调用执行一次有明确缺口列表的定向修复。
3. V1 完整响应只有 JSON/Schema 小范围结构错误时，允许一次“只修结构、不改语义”的修复调用。
4. V1 输出截断、网络中断或整体无法解析时，停止并显式记录失败；不静默退回七场景调用，也不隐藏重试。
5. 已完成的索引和 V1 分别拥有独立检查点；分析版本重启时只恢复未完成阶段。

## 6. 工作沟通边界

### 6.1 可独立成卡的工作沟通

具有可识别工作目的和实质内容的以下交流，各自形成一张卡：

- 会议；
- 拜访；
- 电话或线上沟通；
- 一对一；
- 面试；
- 客户、供应商或合作方交流；
- 有明确工作目的的访谈或节目录制。

纯寒暄、单独的“收到”“知道了”、无实质信息的短通知不构成独立工作沟通卡。

### 6.2 同一次沟通

综合时间连续性、参与关系、沟通形式和上层目的判断。

- 同一次沟通里的多个议题默认合并到一张卡。
- 录音文件切换不等于沟通切换，跨文件的同一次沟通必须合并。
- 短暂中断后回到同一参与关系和上层目的，仍可视为同一次。
- 时间、参与关系、沟通形式或目的出现明确终止与重启时，应分为不同次。

### 6.3 同项目的不同沟通

同一项目在不同会议、拜访或电话中继续推进时，每次沟通分别成卡。卡内可以说明对前一次沟通的承接，但不为了“项目完整”将多次沟通合并成一张卡。

### 6.4 独立工作思考

语音备忘、方案构思和个人工作复盘没有沟通对象，不按“一次工作沟通”自动成卡。只有具有完整方案、重要判断、明确决策、实质风险或持续复用价值时才成卡。

## 7. 其他场景的价值准入

亲子家庭、健康状态、内容消费、灵感洞察、自我成长和生活决策不设数量配额。每张卡必须实质回答至少一个问题：

1. 能帮助用户做出什么更好的判断？
2. 能帮助用户解决什么具体问题？
3. 能帮助用户避免什么真实风险？
4. 保存了什么未来确实需要回顾的信息？
5. 提供了什么只适用于当前情境的帮助？
6. 多段内容是否形成了高于单段复述的新认识？

仅仅“发生过”、包含日期、出现情绪词、可以计算一个简单答案、可以套用一条通用建议，都不构成独立出卡理由。

价值准入是语义决策，不是数量限制。即使某天有超过五张真正高价值的非工作沟通卡，也允许全部保留。

## 8. 主体、对象和表达用途

### 8.1 全局判断维度

对每项将被用于事实还原或分析的内容，必须综合判断：

- 实际表达者是谁；
- 被描述的人、团队或对象是谁；
- 内容是本人经历、转述、引用、案例介绍、媒体播放还是模型输出；
- 当前表达的沟通用途是什么；
- 用户是否明确将第三方案例连接到自己；
- 分析结论的强度是否与证据强度匹配。

### 8.2 不使用场景或关键词一刀切

- 用户在会议中讲述第三方案例，且用于支持产品、项目或业务判断时，这是工作沟通论据，不自动成为对用户本人的分析。
- 用户在朋友聊天、会议或其他语境中明确表达自己的实际状态时，只要内容足够完整，都可用于自我成长或健康状态判断。
- “离职”“疲惫”“犬儒”等词可以触发进一步理解，但关键词本身不能单独证明分析结论。
- 他人对用户的评价在用户没有确认、回应或用具体经历支持时，不写成用户事实。
- 媒体、播客、电视、歌词或模型的表达不自动归属于用户。

### 8.3 禁止单案例硬编码

Prompt、Schema、审核器和清理代码均不得写入针对某个人名、某个事件、某个错误句子或某组心理关键词的生产特判。单案例只能作为测试夹具，用来验证全局规则。

## 9. 卡片内容与表达契约

### 9.1 核心信息区

标题后、第一个二级标题前必须存在核心信息区。它不限于一句话，可以是：

- 一个短结论段；
- 两至三个关键判断；
- 一个结论与一个风险；
- 一个小型结论表；
- “已确定／未确定／下一步”的精简组合。

用户打开卡片后，必须在首屏理解这张卡的重点和价值，不必阅读几段背景后才看到结论。

### 9.2 信息功能

每张卡必须完成与实际内容匹配的四类信息功能，但不强制使用固定栏目名：

1. **核心信息**：最重要的事实、判断、结果或核心问题。
2. **事实与依据**：支持核心信息的必要事实，不按录音顺序复述。
3. **分析与意义**：解释重要性、原因、分歧、权衡、影响、已知与未知。
4. **实质帮助**：提供可用的下一步、表格、模板、话术、清单、决策框架或验证方法；确实不适合建议时，以关键记忆、判断边界或开放问题提供帮助。

### 9.3 结构匹配

| 内容类型 | 优先形式 |
|---|---|
| 多项结论 | 要点列表或结论表 |
| 多个议题 | 分级小标题 |
| 方案比较 | 对比表 |
| 时间推进 | 时间线 |
| 因果关系 | 因果链 |
| 明确行动 | 行动表或步骤 |
| 风险与应对 | 风险—影响—对策表 |
| 沟通建议 | 可复制话术 |
| 复盘建议 | 记录模板 |
| 操作建议 | 分步骤清单 |
| 单一深入判断 | 短段落与证据 |

表格是手段而不是目标。只有一个简单判断时，不强行制作表格；三项以上可横向比较的信息，优先使用表格。

### 9.4 建议质量

“继续保持”“多关注”“继续观察”“加强沟通”“可以记录一下”等原则性文字不能单独构成合格建议。

一项建议至少满足以下两项：

- 明确什么时候做；
- 明确做什么；
- 明确如何记录、验证或判断结果；
- 给出贴合当前情境的例子；
- 给出可直接复制的模板、话术或表格；
- 说明这项建议为什么适合当前问题。

这是所有卡片的全局要求，不是自我成长场景的特例。

### 9.5 禁止报告腔和材料复述腔

正文直接与用户交流，先给判断，再引用必要论据。禁止以下类型的元叙事开头或过渡：

- “这份记录围绕……展开”；
- “从这段记录可以看出……”；
- “你从一段记录中看到……”；
- “分析指向……”；
- “本次材料显示……”；
- “逐字稿中提到……”；
- “你复述分析结论时说……”；
- “该事件反映出……”。

不将这些句子做成简单字符串黑名单，而是由 Prompt 和审核共同检查“是否先复述材料、后给结论”的全局表达模式。确定性清理只处理 Markdown 结构，不机械删除自然语言。

### 9.6 标题与编号

- 模型输出的所有 Markdown 标题不得自带 `一、`、`二、`、`1.`、`1、`、`（一）` 等顺序编号。
- 前端是章节编号的唯一负责方。
- 前端渲染历史 Markdown 时，如已存在手工编号，必须先规范化或禁止再添加一层编号。
- 页面顶部已展示卡片标题，Markdown 一级标题仅用于数据提取，不在正文重复展示。

## 10. 工作沟通卡的内容组织

工作沟通卡不采用空洞的传统会议纪要，但必须让用户快速看清沟通结果。根据实际内容可使用以下功能章节：

1. 核心信息区：本次沟通最重要的结果、判断、风险和下一步。
2. 沟通目的与进展：这次沟通要解决什么，最终推进到哪里。
3. 关键结论：对多议题优先使用“议题／结论／状态”表。
4. 讨论中的关键判断：按议题展开重要依据、分歧和权衡。
5. 尚未解决的问题：只列真正会改变结果的未知项。
6. 行动与跟进：在原始内容已有任务时，以“行动／负责人／时间／目的”呈现。
7. 建议：仅在能提供实质增益时出现，必须具体、可执行并说明适用原因。

不强制每张工作沟通卡拥有全部栏目，但不得将多议题、多结论、多行动堆入一个连续长段落。

## 11. 全局编辑审核

原“跨卡统筹”保留，但更名为“全局编辑审核”，并缩小职责。它不直接生成第二份完整报告，只负责：

1. 对逐字稿分片初审发现的问题做保留、合并、拒绝或修订决策。
2. 检查每次工作沟通是否恰好对应一张卡。
3. 检查同一次沟通是否被拆分，不同沟通是否被合并。
4. 检查主体、表达对象、场景归属和结论强度。
5. 检查非工作沟通卡是否具有独立帮助价值。
6. 检查跨卡和跨场景重复。
7. 生成搜索任务与定向修订任务。

它只输出 `keep`、`revise`、`merge`、`drop`、`create` 等编辑决策、修订要求和搜索任务，不复制所有卡片 Markdown。

## 12. 搜索范围与来源正确性

### 12.1 搜索逻辑冻结

本轮继续使用当前的搜索候选、全局规划、Kimi 2.6 执行与定向修订流程。不以搜索采用率作为本轮验收指标，不为提高采用率强行引用低质量来源。

### 12.2 来源 ID

禁止直接使用可能在不同搜索任务中重复的供应商局部 ID 作为全局 `source_id`。使用规范化 URL 的稳定哈希生成全局 ID：

```text
source_id = "source_" + sha256(canonical_url)
```

同一 URL 在不同任务中应视为同一外部来源；不同 URL 不得相互覆盖。搜索任务与来源的关系另行保留，不编入全局 ID 语义。

无法提供可规范化 URL 的来源不得进入最终发布来源表，但可在内部搜索包中保留作为未发布调试数据。

### 12.3 发布前校验

- 卡片使用的每个 `source_id` 必须在来源表存在。
- 来源标题、域名和 URL 必须对应同一网页。
- 一个 `source_id` 不得在同一发布包中对应多个不同 URL。
- 不存在采用来源的卡片不展示“外部资料”。
- 来源不存在、来源错配或 ID 碰撞直接阻断发布。

## 13. 审核契约

### 13.1 逐字稿分片初审

初审增加：

- 主体和表达对象是否正确；
- 是否把转述、引用、案例或媒体内容当成用户本人经历；
- 是否遗漏了独立工作沟通；
- 工作沟通是否遗漏关键议题、结论和行动；
- 非工作沟通卡是否有独立价值。

### 13.2 终审问题类型

在现有问题类型上新增：

- `wrong_subject`：表达主体或被分析对象错误；
- `wrong_communication_boundary`：同一工作沟通被拆分或不同沟通被合并；
- `low_independent_value`：非工作沟通卡没有独立帮助价值；
- `thin_content`：出卡价值成立，但高价值信息、分析或帮助展开不足；
- `dense_unstructured_body`：复杂内容堆积为大段落；
- `non_actionable_help`：建议仅有原则，缺少执行方法或适配说明；
- `reporting_tone`：使用第三方报告腔或先复述材料后给判断；
- `duplicate_heading_number`：Markdown 标题自带编号且与渲染编号重复；
- `source_registry_mismatch`：卡片声明的来源与发布来源表不一致。

### 13.3 硬阻断项

以下错误不得被综合分数抵消，任一有效问题未解决即阻断发布：

- 把他人经历、状态、观点或决定写成用户的；
- 把用户讲述的第三方案例写成对用户的分析；
- 将同一次工作沟通拆成多张卡；
- 将不同工作沟通错误合并；
- 把模型推断写成录音事实；
- 来源缺失、来源错配或来源 ID 碰撞。

`thin_content`、`dense_unstructured_body`、`non_actionable_help` 和 `reporting_tone` 在终审中也必须全部修正，但它们的修正不得删除已验证的高价值事实或降低内容丰富度。

## 14. Markdown 与前端渲染

1. 后端 Markdown 校验器拒绝带顺序编号的二至四级标题。
2. 前端在自动编号前识别历史手工编号，避免双编号。
3. Markdown 一级标题由数据层提取为卡片标题，详情页正文不重复显示。
4. 表格在窄屏上允许横向滚动，不压缩到无法阅读。
5. 小标题、正文、列表、表格和引用必须具有明确视觉层级。
6. 前端自动编号只是呈现层能力，不改变原始 Markdown 的语义。

## 15. 版本、检查点与恢复

### 15.1 版本隔离

新链路使用新的冻结管线标识 `beta8_indexed_scene_v2`。这个名称明确区分“全量事实索引＋统一完整 V1 写作”与现有“七场景分别生成”。

- `SingleReportRunner` 保持不变。
- 现有 `beta8_multi_scene_v1` 历史分析版本继续按原路由读取。
- 新分析只在明确选择 `beta8_indexed_scene_v2` 时使用新链路。
- 缺失标识的历史行仍解析为旧默认链路；未知标识失败关闭。

### 15.2 检查点

```text
beta8_event_index
beta8_all_scenes_v1
beta8_initial_audit_units
beta8_initial_audit_aggregate
beta8_global_editorial_review
beta8_search_packets
beta8_revised_cards
beta8_final_audit_units
beta8_final_audit_aggregate
beta8_final_candidate
```

每个检查点继续使用输入指纹、上游哈希和 Prompt 指纹决定是否可复用。搜索逻辑虽然不变，但上游卡片和编辑决策变化时仍按现有指纹契约失效下游检查点。

## 16. 运行计量

每次执行生成独立 `run_id`，不再只展示分析版本的累计调用数。计量至少记录：

- 阶段名称；
- 提供商和模型；
- 正常索引、正常 V1 写作、索引修复、结构修复或失败重试；
- 开始、结束和耗时；
- 输入 Token 和输出 Token；
- 本次调用费用，如提供商无法返回则标明不可得；
- Kimi 模型响应次数；
- Web Search 工具调用次数；
- 是否使用了既有检查点。

前端分开显示：

1. 本次正常报告生成；
2. 本次重试和恢复；
3. 本次搜索；
4. 分析版本的历史累计。

正常路径的 V1 阶段调用数必须为 2：一次全量索引和一次完整 V1 写作。索引修复、结构修复和其他重试必须单独显示，不得隐藏到“正常报告生成”中。

## 17. 代码范围

预计修改和新增：

- `backend/src/audio_memory/prompts/beta8/event-index.md`：全量事实索引 Prompt，禁止价值筛选和卡片写作。
- `backend/src/audio_memory/prompts/beta8/all-scenes-v1.md`：统一全场景完整 V1 Prompt，包含完整质量规则、七场景能力和内容结构映射。
- `backend/src/audio_memory/prompts/beta8_composer.py`：组合索引和完整 V1 两次调用请求，两次均注入字节级一致的完整逐字稿。
- `backend/src/audio_memory/prompts/beta8_event_index_schema.py`：定义薄索引、文件覆盖和未覆盖区间契约。
- `backend/src/audio_memory/prompts/beta8_scene_schema.py`：统一场景集合、卡片基础与七场景完整性契约。
- `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`：全局编辑审核、新问题类型和发布校验契约。
- `backend/src/audio_memory/prompts/beta8/unified-audit.md`：主体、沟通边界、价值、丰富度、结构、帮助和表达检查。
- `backend/src/audio_memory/prompts/beta8/cross-card-orchestration.md`：改为全局编辑审核 Prompt，保留搜索规划职责。
- `backend/src/audio_memory/prompts/beta8/targeted-revision.md`：支持新的修订要求，不在结构修正中损失内容。
- `backend/src/audio_memory/analysis/beta8_event_index.py`：验证索引 ID、文件覆盖、区间闭包和边界冲突。
- `backend/src/audio_memory/analysis/beta8_quality.py`：负责 Markdown 结构、具体建议、报告腔和呈现形式的确定性检查。
- `backend/src/audio_memory/analysis/beta8_runner.py`：将七场景并行调用改为“全量索引＋统一完整 V1”两次调用，加入独立检查点和显式失败恢复。
- `backend/src/audio_memory/analysis/beta8_search.py`：规范化来源 ID，不修改搜索策略。
- `backend/src/audio_memory/analysis/beta8_cleanup.py`：确定性 Markdown 结构清理。
- `backend/src/audio_memory/analysis/beta8_evaluation.py`：新的质量和人工评审指标。
- `backend/src/audio_memory/analysis/publisher.py`：发布前来源一致性和卡片契约校验。
- `backend/src/audio_memory/analysis/pipeline_state.py`：新管线版本、检查点和分运行计量。
- `prototype/src/api/state.js`：Markdown 标题规范化和运行计量展示。
- `prototype/src/App.jsx`：防止重复编号，保持表格和层级呈现。
- `prototype/src/styles.css`：仅补足新结构必需的阅读样式，不扩展为整体 UI 重设计。

实施必须保护当前脏工作树：不重置、不清理、不覆盖、不合并、不提交、不推送，除非后续得到用户对对应操作的明确授权。

## 18. TDD 范围

### 18.1 先固定失败样本

在生产代码修改前先增加失败测试：

1. 一次周会讨论多个议题，只能生成一张工作沟通卡。
2. 同一项目在两次独立沟通中讨论，必须生成两张卡。
3. 一次会议和一次供应商拜访必须分开。
4. 用户在工作沟通中介绍第三方案例，案例不得成为对用户本人的自我分析。
5. 用户在聊天或会议中明确讲述自己的状态，且证据足够时，允许生成自我成长或健康分析。
6. 关键词相同但表达对象不同的两个样本，必须得到不同归属。
7. 只有日期计算且没有独立帮助价值的内容不成卡。
8. 超过五张真实高价值的非工作沟通卡允许全部保留，验证不存在隐性数量上限。
9. 复杂卡片不得只有一个连续长段落。
10. 原则性建议没有执行方法、示例或适配说明时不通过。
11. 以“这份记录围绕……”开始、先复述后判断的内容不通过表达审核。
12. 标题手工编号与前端编号不得重复。
13. 不同搜索任务返回相同供应商局部 ID 时，不同 URL 仍必须得到不同全局 `source_id`。
14. 卡片来源与发布来源表不一致时必须阻断发布。
15. 全量索引不做价值筛选，播客、媒体播放、第三方案例和低价值实质单元仍需登记。
16. 索引未通过文件与区间覆盖门禁时，不得进入 V1 写作。
17. 正常全场景 V1 路径恰好两次提供商调用，且两次都获得完整逐字稿。
18. 已有可复用索引检查点时不重跑索引；已有 V1 检查点时不重跑任一 V1 调用。
19. 索引或 V1 整体失败时显式停止，不得静默执行七次。
20. 每次运行的正常调用、结构修复、索引修复、时间、Token 和费用独立可查。

### 18.2 实施顺序

1. 失败夹具与 Prompt/Schema 契约测试。
2. 全量事实索引 Schema、Prompt 与覆盖门禁。
3. 统一全场景完整 V1 Schema 与 Prompt。
4. Runner 两次调用、独立检查点和显式失败恢复。
5. 工作沟通边界、主体、价值和跨场景审核。
6. 丰富度、结构化呈现、建议和表达审核。
7. Markdown 双编号与前端兼容。
8. 搜索来源 ID 和发布一致性。
9. 分运行计量与页面展示。
10. 完整回归与真实逐字稿评测。

## 19. 验收标准

### 19.1 自动化验收

- 七场景输出恰好完整且不重复。
- 正常 V1 路径的 DeepSeek 调用次数恰好为 2，分别为全量索引和完整 V1 写作。
- 同一次工作沟通的多议题在同一张卡。
- 不同工作沟通分别成卡。
- 无非工作沟通卡数量上限。
- 第三方案例、媒体和他人状态不会因关键词被归为用户状态。
- 用户在任何语境中的真实自我表达仍可被正确分析。
- Markdown 无手工顺序编号和双编号。
- 搜索来源 ID 不碰撞，卡片引用与来源表一致。
- `SingleReportRunner` 和历史管线回归通过。

### 19.2 真实逐字稿验收

使用本次评审的同一份豆包长录音 2.0 合并逐字稿，在全新分析版本中运行，不复用原累计 105 次调用的版本。验收要求：

1. 正常情况下两次调用产出七场景完整 V1：全量索引一次，完整 V1 写作一次。
2. 全量索引必须覆盖四份豆包录音中的所有实质内容，包括此前 X 方案遗漏的播客内容；不准在索引阶段因价值不足删除。
3. 所有可识别的独立工作沟通各对应一张卡，必须先由人工建立本次样本的工作沟通 Ground Truth，再对比模型结果。
4. 同一沟通不再按模型切换、OTA、负责人、文档或资源等议题拆分。
5. 用户在工作沟通中讲述的第三方案例不被绑定成用户自身状态；如用户另有明确自我表达，仍按完整语义独立判断。
6. 只有简单日期计算且没有独立价值的内容不单独成卡。
7. 不通过固定卡片配额删除真实高价值卡。
8. 每张保留卡的核心信息在首屏可识别，内容足够丰富，且结构与内容匹配。
9. 复杂卡片使用有意义的小标题、表格、列表、时间线、因果链、步骤或模板，不以一整段呈现，且呈现形式必须与内容类型匹配。
10. 有建议的卡片提供可执行方法、示例、话术、清单、表格或验证方法，而不是只有原则。
11. 正文先给核心判断，再呈现论据，不使用“这份记录”“本次材料”等第三方报告腔。
12. 页面不出现重复主标题或 `1 一、` 类双编号。
13. 搜索的产生、数量和采用逻辑与本轮前保持一致。
14. 所有最终展示的搜索来源都与引用正文一一对应，不存在跨任务 ID 覆盖。
15. 每个阶段的调用次数、时间、Token、重试和搜索用量独立可查。
16. 主体错误、工作沟通边界错误、未解决的事实错误和来源错配为零。

## 20. R01–R25 实现反向追溯（截至 2026-09-06）

本表最初建立时的 Task 13 基线证据是 2026-09-04 的 Beta 8 后端 `293 passed`、历史路由/协调/重分析 `95 passed`和前端卡片与计量 `10 passed`；该基线不包含后续 final-fix 测试。表中后来增补的 R05/R22 代码与测试，最新非付费证据来自 2026-09-06 final-fix round 4 实施回归：Beta 8 相关套件 `366 passed`，全后端 `1744 passed, 28 skipped, 1 environment-only failed`（托管沙箱禁止 loopback bind）；该 loopback 用例后续在允许本地端口绑定的环境中单独通过。表中的“自动化通过”只说明实际 Prompt、生产代码和验收测试已经闭合；凡涉及真实模型内容质量、真实 Kimi 搜索或浏览器观感的项目，都明确保留为待授权验收，不能用单元测试代替。Round 4 独立只读复审结论为 PASS（0 Critical / 0 Important），报告为 `/private/tmp/beta8-final-branch-r4-review.md`。

| ID | 实际运行 Prompt 路径 | 生产代码路径 | 具体通过测试 | 截至 2026-09-06 最新非付费验证结果 |
|---|---|---|---|---|
| R01 | `backend/src/audio_memory/prompts/beta8/event-index.md`；`backend/src/audio_memory/prompts/beta8/all-scenes-v1.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/analysis/beta8_runner.py`；`backend/src/audio_memory/analysis/beta8_state.py` | `test_runner_calls_event_index_then_complete_v1_exactly_once_each`；`test_end_to_end_fake_provider_runs_ordered_stages_and_publishes_once` | 自动化通过：假提供商正常 V1 恰好两次。第二次真实尝试确实依次发生 `event_index` 和 `all_scenes_v1` 两次调用，但后者未通过 V1 检查点，因此仍无可验收报告。 |
| R02 | `backend/src/audio_memory/prompts/beta8/event-index.md` | `backend/src/audio_memory/prompts/beta8_event_index_schema.py`；`backend/src/audio_memory/analysis/beta8_event_index.py` | `test_event_index_schema_is_thin_and_forbids_value_selection_fields`；`test_event_index_covers_every_segment_exactly_once_in_transcript_order` | 自动化通过；第二次真实尝试已成功保存覆盖门禁后的薄索引，产物 SHA-256 为 `3d8baf8df18c323732fced16c09991ca7ab935ac04dd8762ec135c2a15334971`。 |
| R03 | `backend/src/audio_memory/prompts/beta8/event-index.md`；`backend/src/audio_memory/prompts/beta8/all-scenes-v1.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/analysis/beta8_runner.py` | `test_both_v1_calls_receive_identical_full_transcript`；`test_runner_calls_event_index_then_complete_v1_exactly_once_each` | 自动化通过：两请求使用同一完整逐字稿。 |
| R04 | `backend/src/audio_memory/prompts/beta8/all-scenes-v1.md`；`backend/src/audio_memory/prompts/beta8/scenes/*.md` | `backend/src/audio_memory/prompts/beta8_scene_schema.py`；`backend/src/audio_memory/analysis/beta8_runner.py` | `test_unified_v1_requires_exactly_seven_ordered_scenes`；`test_unified_scene_outputs_keep_existing_downstream_shapes` | 自动化通过：七场景完整且 Markdown 直出；真实可读性待人工验收。 |
| R05 | `backend/src/audio_memory/prompts/beta8/event-index.md`；`backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/all-scenes-v1.md`；`backend/src/audio_memory/prompts/beta8/cross-card-orchestration.md` | `backend/src/audio_memory/prompts/beta8_scene_schema.py`；`backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_privacy.py`；`backend/src/audio_memory/analysis/beta8_runner.py`；`backend/src/audio_memory/analysis/publisher.py` | `test_work_communication_unit_cannot_be_omitted`；`test_orchestration_cannot_drop_work_communication_card`；`test_orchestration_cannot_merge_distinct_work_communication_units`；`test_internal_work_unit_ids_never_enter_any_model_payload`；`test_v1_downstream_privacy_leaks_fail_before_checkpoint_or_next_model`；`test_json_unicode_escape_cannot_hide_reserved_mapping_value`；`test_unicode_escaped_audit_leak_writes_zero_audit_checkpoints`；`test_audit_repair_privacy_leak_is_not_persisted_or_forwarded`；`test_orchestration_repair_privacy_leak_is_not_replayed_or_persisted`；`test_unparseable_orchestration_output_is_not_replayed_to_repair`；`test_percent_decoded_restored_search_leak_writes_zero_checkpoint`；`test_indexed_publish_derives_work_units_from_checkpoint_before_side_effects` | 非付费确定性闭包与模型载荷隐私门禁通过；JSON Unicode 解码、audit/orchestration 正常与修复输出、不可解析输出不回灌、搜索 URL 规范化前后均已闭合；真样本 5 次沟通的语义边界待单方案人工质量验收。 |
| R06 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/all-scenes-v1.md` | `backend/src/audio_memory/prompts/beta8_scene_schema.py` | `test_non_work_scene_has_no_card_count_cap`；`test_every_index_unit_is_used_or_explicitly_omitted` | 自动化通过：无数量上限且遗漏必须显式；真实价值判断待人工验收。 |
| R07 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/event-index.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md` | `backend/src/audio_memory/prompts/beta8_event_index_schema.py`；`backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_audit.py` | `test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only`；`test_audit_supports_all_report_quality_issue_types` | 自动化 Prompt/审核契约通过；真实主体归属错误数待人工质量验收确认。 |
| R08 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md`；`backend/src/audio_memory/prompts/beta8/targeted-revision.md` | `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_audit.py`；`backend/src/audio_memory/analysis/beta8_quality.py` | `test_v1_audit_and_revision_embed_exact_shared_quality_contract`；`test_audit_supports_all_report_quality_issue_types` | 运行契约和 `thin_content` 审核类型通过；真实单卡丰富度待人工质量验收。 |
| R09 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/all-scenes-v1.md` | `backend/src/audio_memory/analysis/beta8_quality.py`；`backend/src/audio_memory/analysis/beta8_cleanup.py`；`prototype/src/api/state.js`；`prototype/src/App.jsx` | `test_accepts_core_information_block_with_multiple_judgments`；前端 `derives the first model H1 for the card header without leaving it in the rendered body` | 自动化通过；浏览器首屏观感待人工验收。 |
| R10 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md`；`backend/src/audio_memory/prompts/beta8/targeted-revision.md` | `backend/src/audio_memory/analysis/beta8_quality.py`；`prototype/src/App.jsx`；`prototype/src/styles.css` | `test_shared_contract_contains_every_presentation_mapping`；`test_reports_structures_without_claiming_semantic_correctness`；前端 `provides markdown tables to the renderer through an accessible horizontal-scroll wrapper contract` | 结构映射契约、结构识别和表格渲染通过；内容与结构的语义匹配仍需真实报告人工判断。 |
| R11 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md`；`backend/src/audio_memory/prompts/beta8/targeted-revision.md` | `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_runner.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_audit_supports_all_report_quality_issue_types` | `non_actionable_help` 契约通过；真实建议是否具体待人工质量验收。 |
| R12 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md` | `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_quality.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_audit_supports_all_report_quality_issue_types` | `reporting_tone` 契约通过；真实正文语气待人工质量验收。 |
| R13 | `backend/src/audio_memory/prompts/beta8/scenes/work-communication.md`；`backend/src/audio_memory/prompts/beta8/scenes/parenting-family.md`；`backend/src/audio_memory/prompts/beta8/scenes/health-state.md`；`backend/src/audio_memory/prompts/beta8/scenes/content-consumption.md`；`backend/src/audio_memory/prompts/beta8/scenes/inspiration-insight.md`；`backend/src/audio_memory/prompts/beta8/scenes/self-growth.md`；`backend/src/audio_memory/prompts/beta8/scenes/life-decisions.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/analysis/pipeline_identity.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only`；`test_prompt_manifest_contains_every_runtime_prompt` | 七场景 Prompt、语义夹具和哈希绑定通过；人工场景评审待授权运行后完成。 |
| R14 | `backend/src/audio_memory/prompts/beta8/scenes/parenting-family.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/prompts/beta8_pipeline_schema.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only` | 自动化 Prompt/审核契约通过；真实互动分析与安全边界待人工验收。 |
| R15 | `backend/src/audio_memory/prompts/beta8/scenes/health-state.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/prompts/beta8_pipeline_schema.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only` | 自动化 Prompt/审核契约通过；真实低强度信号覆盖与无诊断待人工验收。 |
| R16 | `backend/src/audio_memory/prompts/beta8/scenes/content-consumption.md`；`backend/src/audio_memory/prompts/beta8/event-index.md`；`backend/src/audio_memory/prompts/beta8/report-quality.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/prompts/beta8_event_index_schema.py` | `test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only`；`test_event_index_schema_is_thin_and_forbids_value_selection_fields` | 自动化通过；真样本媒体单元完整性待人工对 Ground Truth。 |
| R17 | `backend/src/audio_memory/prompts/beta8/scenes/inspiration-insight.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/prompts/beta8_pipeline_schema.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only` | 自动化 Prompt/审核契约通过；真实发散与验证收敛质量待人工验收。 |
| R18 | `backend/src/audio_memory/prompts/beta8/scenes/self-growth.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/prompts/beta8_pipeline_schema.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only` | 自动化 Prompt/审核契约通过；真实证据强度和实质工具待人工验收。 |
| R19 | `backend/src/audio_memory/prompts/beta8/scenes/life-decisions.md`；`backend/src/audio_memory/prompts/beta8/unified-audit.md` | `backend/src/audio_memory/prompts/beta8_composer.py`；`backend/src/audio_memory/prompts/beta8_pipeline_schema.py` | `test_all_scenes_v1_instructions_preserve_core_and_scene_specific_capabilities`；`test_all_scenes_v1_encodes_scene_semantic_cases_in_instructions_only` | 自动化 Prompt/审核契约通过；真实直接/条件化建议待人工验收。 |
| R20 | `backend/src/audio_memory/prompts/beta8/cross-card-orchestration.md`；`backend/src/audio_memory/prompts/beta8/targeted-revision.md` | `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_runner.py` | `test_keep_card_final_hash_equals_v1_hash`；`test_keep_card_is_never_sent_to_revision_model`；`test_every_input_card_is_consumed_exactly_once` | 自动化通过：keep 原文不变，仅 revise/merge/create 进入修订。 |
| R21 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/cross-card-orchestration.md`；`backend/src/audio_memory/prompts/beta8/targeted-revision.md` | `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_runner.py`；`backend/src/audio_memory/analysis/publisher.py` | `test_every_todo_candidate_is_kept_or_dropped_once`；`test_beta8_publish_reconciles_global_todos_once` | 自动化通过：每个候选保留或有丢弃原因；真实承诺识别待人工验收。 |
| R22 | `backend/src/audio_memory/prompts/beta8/cross-card-orchestration.md`；`backend/src/audio_memory/prompts/beta8/search-execution.md`；`backend/src/audio_memory/prompts/beta8/targeted-revision.md` | `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`；`backend/src/audio_memory/analysis/beta8_search.py`；`backend/src/audio_memory/analysis/beta8_runner.py`；`backend/src/audio_memory/analysis/publisher.py` | `test_search_task_count_and_adoption_policy_are_unchanged`；`test_search_policy_projection_does_not_add_pre_task8_rejections`；`test_same_provider_local_id_with_different_urls_remains_distinct_across_tasks`；`test_beta8_publish_rejects_conflicting_source_registry_before_audio_move_or_database_write` | 仓库内 Task 8 前 v2 fixture 以代表性候选与记录统筹输出作为 input-to-expected oracle；生产 runner 调用的同一投影仅观察旧策略实际结果，不新增重复 key、规范化问题、unknown target 或 adoption mismatch 失败语义；已覆盖候选聚合、任务数/顺序、target/adoption、0/多任务、approved-only packet 投递和 Kimi 2.6 request shape。来源稳定 ID 和发布闭包为唯一允许差异，真实 Kimi 联网仍待授权。 |
| R23 | `backend/src/audio_memory/prompts/beta8/report-quality.md`；`backend/src/audio_memory/prompts/beta8/all-scenes-v1.md` | `backend/src/audio_memory/analysis/beta8_quality.py`；`backend/src/audio_memory/analysis/beta8_cleanup.py`；`prototype/src/api/state.js`；`prototype/src/App.jsx`；`prototype/src/styles.css` | `test_rejects_manual_ordinals_and_duplicate_h1_with_locations`；前端 `normalizes historical manual ordinals in H2 through H4 without altering decimals or paragraph text`；前端 `provides markdown tables to the renderer through an accessible horizontal-scroll wrapper contract` | 自动化通过；真实浏览器标题/窄屏表格验收待授权运行后完成。 |
| R24 | 不适用（运行计量契约，无模型 Prompt） | `backend/src/audio_memory/analysis/pipeline_state.py`；`backend/src/audio_memory/analysis/beta8_runner.py`；`backend/src/audio_memory/api/jobs.py`；`prototype/src/api/state.js`；`prototype/src/App.jsx` | `test_metrics_separate_v1_repairs_audits_and_revisions_by_run`；`test_resume_metrics_count_every_actual_model_call_once`；前端 `groups current normal calls, repairs, search, and historical totals separately` | 自动化通过：本次/历史、阶段、修复、搜索、空费用和终审分数分开；浏览器显示待人工验收。 |
| R25 | 不适用（验收输入契约，无模型 Prompt） | `backend/src/audio_memory/analysis/beta8_evaluation.py`；`tests/evaluate-beta8-indexed-v1.py` | `test_manifest_requires_fixed_asr_identity_counts_and_sha256`；`test_prepare_dry_run_is_offline_and_does_not_create_output_directory`；`test_paid_cli_refuses_run_without_confirmed_before_creating_output`；`test_failed_paid_run_is_persisted_and_clears_running_state`；`test_resume_paid_run_requires_same_bound_inputs_and_saved_index`；`test_resume_preflight_reclaims_a_failed_checkpointed_version`；`test_validation_tier_binds_flash_smoke_without_qualifying_as_final` | 离线输入验收通过：豆包/录音文件识别 2.0、4 文件、6373 段、逐字稿与 Ground Truth 哈希匹配且无写入。三次 V4 Pro 尝试和一次独立 Flash 机械链路尝试均已保存失败证据；Flash 产物不具备最终验收资格，本次在索引输出 32,000 Token 时截断。确定性结构归位、Flash 验证层级隔离和 Flash-only 64k 索引容量均已通过非付费回归，最终验收仍未完成。 |

非付费实现没有发现“仅存在于设计文档、没有进入实际 Prompt/代码”的 R 项。Task 6 修复范围的独立复审已完成，报告 `/private/tmp/beta8-task-6-final-review.md` 结论为 PASS、0 Critical、0 Important；此前因审查服务 HTTP 404 保留的非付费门禁由此关闭。最终整分支复审已将原 8 项 open deferred Minor 裁决为 1 项 `resolved later`、7 项 `accepted boundary`、0 项 fix-now；详细理由见 `/private/tmp/beta8-final-branch-r4-review.md`。除此之外，跨 R05–R19、R21–R25 的真实模型/浏览器人工验收，以及 R22 的真实联网结果验证仍待授权，不由文档或自动化结果冒充完成。

## 21. 实施完成条件

本设计只有在以下证据同时成立时才算实施完成：

1. 目标单元、契约、集成、前端和回归测试通过。
2. 新管线的实际运行状态、检查点和指标持久化正确。
3. 开发环境页面可正确打开最终报告。
4. 同一份真实逐字稿完成一次全新运行，并通过本文第 19.2 节的人工验收。
5. 运行报告同时提供生成时间、分阶段耗时、Token、费用、调用次数和最终评审结果。
6. 不影响 `SingleReportRunner`、历史卡片读取、转写和生产环境。
7. 当前脏工作树中与本任务无关的文件保持不变。
8. Task 6 修复范围的独立复审已通过；最终整分支审查逐项裁决所有仍为 `open` 的 deferred minor。
