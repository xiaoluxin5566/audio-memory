# Beta 8 真实工作沟通边界修复

## 结论

针对真实 Pro 索引把 Ground Truth 中 5 次工作沟通错误建模为 18 个 `work_communication` 单元的问题，已完成不联网、不调用模型的 Prompt 根因修正和验收前置硬门禁。

## 根因修正

事件索引 Prompt 现在明确要求：

- 先识别一次真实沟通的开始与结束，再登记内部议题；
- 同一次会议不得因 OKR、产品方向、隐私、功耗、商业模式等议题变化而拆分；
- 同一次沟通跨录音文件时使用同一个单元和多个范围；
- 两次独立电话即使相邻、项目相同也必须分开；
- 谈到工作不等于工作沟通；没有推进任务、对齐决策、明确责任或解决问题等工作目的的职业和生活聊天使用其他 kind。

运行时 Prompt 与批准源副本保持字节级一致。

## Ground Truth 前置门禁

正式验收运行器新增可注入的事件索引门禁。验收入口将哈希绑定的 Ground Truth 交给门禁，并在任何 V1 调用前验证：

1. `work_communication` 单元数与真实沟通数一致；
2. 每次真实沟通只能匹配一个索引单元；
3. 不同真实沟通不能匹配同一个索引单元；
4. 不允许存在无法匹配真实沟通的额外工作沟通单元；
5. 跨文件真实沟通的全部范围必须由同一个索引单元覆盖。

门禁失败时不会调用 `all_scenes_v1`。新生成的索引原始响应会先以 `event_index` 阶段名写入本地隔离目录，便于在不重新付费的情况下诊断。

## 真实旧索引验证

已将此前真实 18 单元 Pro 索引送入新门禁，结果确定性拒绝：

`Ground truth communication boundary mismatch: expected 5 work communications, got 18`

## 哈希边界

- 有界事件索引 Prompt SHA-256：`94a9d77a49355433894e14442532fbfff419d1810ba0521a4c9ffba32593e71f`
- 有界固定规则 SHA-256：`1a676f3716eece19bdf3ca1a0891d73d1064d3b0ec550bae8271b87b8d7d6028`
- 上一版边界修复 Prompt SHA-256：`415c805fa823c4d86997cb77033b97dec21008ecdad906ea598b0dd0703c98d3`
- 上一版边界修复固定规则 SHA-256：`1c57f6671b283a507668dbcf1f60af2b14a37c80df783669825f4c6b853ef396`
- 旧固定规则 SHA-256：`a9b2cceb92591e22b8637d2a48ac93061ff850b86966ea8190c0c236a5490cfa`
- 旧索引与旧 V1 检查点继续原样保留作为失败证据，但不会被新规则误复用。

## 索引后受控暂停

验收入口新增 `--stop-after-index`，并与 `--stop-after-v1` 互斥。启用后，运行顺序固定为：生成索引、隔离保存原始响应、完成结构与 Ground Truth 沟通边界门禁、保存 `beta8_event_index` 检查点，然后以 `checkpoint_stage=event_index`、`resumable=true` 受控暂停。不会调用统一 V1、审核、搜索、修订或发布。

## 验证证据

- TDD 覆盖正确一对一映射、额外误分类、总数相同但同时误合并与误拆分、V1 前停止、索引原文先隔离保存。
- Beta 8 全相关回归：`390 passed, 1417 deselected`。
- 离线 dry-run：通过，`network_accessed=false`、`writes_performed=false`。
- 运行时 Prompt 与批准源副本：字节级一致。
- `git diff --check`：通过。
- 上述本地修复和验证阶段未调用付费模型；后续单步实跑记录见下节。全程未执行搜索、审核、修订、发布、提交或推送。

## 2026-09-07 Pro 索引单步验收

在用户明确授权后，使用 `--stop-after-index` 进行了一次真实 Pro 索引调用。该调用以 235,732 输入 token 和 32,000 输出 token 结束，输出精确命中上限，错误码为 `model_output_truncated`。因此 Ground Truth 边界门禁未执行，也未保存可复用索引检查点。本次没有调用 V1、审核、搜索、修订或发布。

该实跑暴露出一个独立缺口：供应商客户端在截断时已拿到部分文本，但旧错误对象没有携带它，导致运行器无法将部分响应写入隔离目录。现已增加安全部分响应传递与 `<scene_id>_truncated` 隔离保存，同时保证原文不进入错误消息、错误表示或诊断日志。

截断留存修复后，供应商客户端回归为 `14 passed`，Beta 8 全相关回归为 `384 passed, 1417 deselected`，离线 `--stop-after-index` dry-run 为 `network_accessed=false`、`writes_performed=false`，`git diff --check` 通过。完整后端回归为 `1768 passed, 28 skipped, 5 failed`：其中 4 项是已有 Flash 模型目录与旧“只允许 Pro”测试的冲突，1 项是沙箱禁止绑定本地端口；均不涉及本次部分响应传递路径。

完整记录见 `outputs/beta8-indexed-v1/run-44768360-22a7-498f-b413-0d120fedc5df/acceptance-attempt-8-pro-index-truncated.md`。

## 索引输出有界化修复

本次 32,000 Token 截断证明，原契约中的“薄索引”只是软性文字要求，Schema 仍允许 10,000 个 units、每个 unit 2,000 个 ranges 和数千字符描述。边界修复仅约束了工作沟通，没有约束媒体播放、家庭互动和其他连续活动的过度拆分。

现在的有界契约为：

- 全部 kind 均按连续真实活动建单元，不按话题、句子、片段或短视频切换拆分；
- 同一文件的相邻片段必须合并为一个 range；
- 最多 128 个 units、每个 unit 最多 32 个 ranges、最多 128 个 excluded ranges；
- `subject` 最长 120 字符，`description` 最长 300 字符且只允许一句中性概述；
- 真实事件确实超限时不允许错误合并，必须以 `event_index_limit_exceeded` 短失败结束，不得继续生成到模型上限。

新 Schema 已对超过上述数量和文字长度的输出强制拒绝。历史真实成功索引以 52 个 units、3 个 excluded ranges 重新验证通过。最新 Beta 8 回归为 `390 passed, 1417 deselected`，离线索引单步 dry-run 通过且 `network_accessed=false`、`writes_performed=false`。

## 有界索引真实 Pro 验证

在用户明确授权后，有界索引以一次 `deepseek-v4-pro` 调用完成返回：输入 236,053 Token，输出 6,874 Token，未截断。输出为 52 个 units 和 3 个 excluded ranges，通过 JSON、Schema 和逐片段覆盖门禁。

模型仍将 Ground Truth 中 5 次真实工作沟通输出为 21 个 `work_communication` 单元，因此语义边界门禁拒绝该索引，未保存检查点，也未调用 V1 或任何下游阶段。这一结果证明，继续只通过增强同一个大 Prompt 解决语义边界已不是可靠路径。

完整记录见 `outputs/beta8-indexed-v1/run-119d307d-a1b7-45e8-9dd9-edabe50ea77c/acceptance-attempt-9-pro-index-bounded.md`。

## 活动会话索引契约修正

21 个工作沟通单元的实际分解表明，旧 `kind` 同时混合了活动形式、话题内容和报告场景：午餐职业闲聊被误当成工作沟通，同一场跨文件会议被按议题拆分，两次独立电话又被合并。

新契约改为：

- 顶层输出为 `activity_sessions`，一项代表一次连续真实活动，不是一个话题；
- `activity_kind` 只区分对话、内容播放、独自表达或活动、环境活动和其他活动；
- `communication_purpose` 只在对话中区分 `work`、`non_work`、`mixed` 和 `uncertain`；
- 只有 `conversation + work/mixed` 进入“一次真实工作沟通对应一张卡”的闭包；
- 非对话活动必须使用 `communication_purpose=not_applicable`，Schema 直接拒绝矛盾组合；
- 七场景归属仍由第二次 V1 结合完整逐字稿判断，索引不恢复场景分类权。

旧 21 单元响应因仍使用顶层 `units` 和 `kind` 而被新 Schema 确定性拒绝，不会被隐式迁移或误复用。当前哈希边界为：

- 活动会话索引 Prompt SHA-256：`ecbefdf4492560e216a100b359d70e25ee5de1bbd8f1a7b0a3ba5f9d17b8f28f`；
- 全场景 V1 Prompt SHA-256：`8abca1f074fc1a262cbf781abb2fc9ef771830c3cc6f77a6c2ac488046958329`；
- 共享质量契约 SHA-256：`4661a998f22ac346db3d9191c72920cec4a93229afd07890543e04472b910cbf`；
- 固定规则 SHA-256：`4ff72e9d94f3032d2110ff723fc9694aa9ff59ab774b48d6374bf58ca57024ac`。

本地验证为 `396 passed, 1417 deselected`；索引单步 dry-run 为 `network_accessed=false`、`writes_performed=false`；运行时 Prompt 与批准源副本字节级一致。本次契约修正没有调用付费模型、搜索、发布、提交或推送。真实输入能否从 21 改善为 5，需要另行明确授权后才能用 Pro 单步索引验证。

`--stop-after-index` 现在还具有付费调用硬保护：如果首次索引存在覆盖缺口，单步验收会立即失败并保留已隔离的原始响应，不会自动发起第二次 `index_repair` 付费调用。两层索引上线后，正常完整链路也不再隐式执行索引修复；若未来需要恢复，必须作为独立费用策略显式配置和授权。

## 活动会话索引真实 Pro 验证

在用户明确授权后，使用 `--stop-after-index` 只执行了一次 `deepseek-v4-pro` 索引调用：输入 236,244 Token，输出 3,821 Token，模型耗时 37,888 ms，总耗时 38,175 ms。供应商未返回金额字段。输出未截断，但 Ground Truth 门禁得到 8 次工作沟通而非 5 次，因此未保存可复用检查点，也未调用 V1、审核、搜索、修订或发布。

原始响应保留在 `outputs/beta8-indexed-v1/run-40319192-9588-4a08-bdf6-25e6024c3dc7/quarantine/event_index-6d597523-4d50-41e3-83b7-b50c5873bb63.provider-response.json`。八个工作单元的确定性分解为：

- 1 个预录创始人访谈播放被误判为用户实时工作沟通；
- 2 次前段工作沟通识别正确；
- Ground Truth 中 2 次相邻车辆电话被合并为 1 次；
- Ground Truth 中 1 场跨文件工作坊被按录音文件和内部议题拆成 4 次。

算术上为“1 个误报 + 2 个正确 + 1 个误合并 + 4 个误拆分 = 8”；删除预录访谈、将车辆电话一分为二、将工作坊四合为一后，才是 Ground Truth 的 5 次。

## 实时参与与会话起止二次修正

8 次结果证明活动形式与沟通目的分层是有效的，但还缺少两个显式轴：用户是否亲自实时参与，以及这次联系事实上从何处开始、何处结束。本地修正新增：

- `participation_mode`：区分用户实时交互、预录/广播内容、用户独自活动、环境/第三方活动和未知；
- `start_boundary` 与 `end_boundary`：对每个会话要求结构化的真实起止信号；
- Schema 强制 `conversation` 必须为 `user_present_live_interaction`，媒体播放必须为 `recorded_or_broadcast_content`，并拒绝以普通活动开始/结束冒充对话边界；
- Prompt 明确预录访谈或播客的问答不是用户实时沟通；话题、项目、报告场景和录音文件变化不是会话边界；在前一联系已结束的前提下，新问候/身份确认可作为下一电话开始证据。

V1 和共享质量契约同步将工作沟通定义收紧为 `conversation + user_present_live_interaction + work/mixed`，防止下游将媒体访谈重新当成工作卡。该修正尚未进行新的付费调用，因此只能确认契约与链路本地成立，不声称真实模型已经达到 5 次。

二次修正后的当前哈希为：

- 活动会话索引 Prompt SHA-256：`72e58e110ddfaa3d238df4e0d244e4433afdedd5bc13efcd19c6654952e979c3`；
- 全场景 V1 Prompt SHA-256：`2c7902e061605f646febd2f20d808e443486dfa0147d941a136208ba6cbc0f3b`；
- 共享质量契约 SHA-256：`97c0fc57f807959303eed60e1da4ffdb4ca1718fa3048891f479736f9de07cce`；
- 固定规则 SHA-256：`eaa0c7cfffb23af6616814bfe1a35464c62cc7a657e2ca8bdd90f4417d8688a2`。

本地验证结果：Beta 8 全相关回归 `400 passed, 1417 deselected`；索引单步 dry-run 为 `network_accessed=false`、`writes_performed=false`；三组运行时 Prompt 与批准源副本均字节级一致；`git diff --check` 通过。

## 实时参与修正后的第二次 Pro 索引验证

用户再次明确授权后，使用 `--stop-after-index` 只执行了一次 `deepseek-v4-pro` 请求。运行目录为 `outputs/beta8-indexed-v1/run-cb2c7b41-8e85-4e93-8996-c74db5574b4c`，计量为：

- 输入 236,635 Token，输出 7,077 Token；
- 模型耗时 70,898 ms，总耗时 71,131 ms；
- `model_call_count=1`，未截断，供应商未返回费用金额；
- 未调用 V1、审核、搜索、修订或发布，也没有自动追加索引修复。

原始响应大小为 25,246 bytes，SHA-256 为 `165433e5fbd76218f653abfbffa2d069375596ed920d658ed079647178fa61d7`，已以 `0600` 私有文件保留在 `quarantine/event_index-cb5429ce-351b-4613-add8-e06047988153.provider-response.json`。

该响应生成 37 个活动会话。原 Schema 首先因 8 个面对面普通交谈使用 `activity_started/activity_ended` 而拒绝输出。离线复核证明这是本地 Schema 过严：当面交谈不一定存在电话式接通/挂断信号。放宽这一错误限制后，响应能通过 JSON 与 Schema，但仍存在两个真实质量问题：

1. `seg_3_820` 被前一媒体播放和后一游戏活动同时包含，违反“每个片段恰好覆盖一次”；
2. 工作沟通候选为 6 次而非 Ground Truth 的 5 次。预录访谈误报已消失，但两次车辆电话仍合并为 1 次，同一场跨文件工作坊仍拆成 3 次。其组成为“2 次正确 + 1 次误合并 + 3 次误拆分 = 6”。

因此这次调用没有产生可复用索引检查点，也不能声称已达到 5 次。

## 下次付费尝试前的索引与 V1 全链路审查

根据用户要求，已暂停付费尝试，对“索引生成 → Schema → 覆盖/Ground Truth 门禁 → 检查点 → 统一 V1 → V1 结构/隐私门禁 → 失败留存与计量”做了端到端审查。发现并修正：

1. 面对面交谈可使用 `activity_started/activity_ended`，Schema 不再要求伪造接通/挂断信号；
2. 索引 Prompt 增加两遍边界复核：先扫描每个 range 内部的重新联系信号，再反向合并仅因文件、演示/讨论阶段或议题变化而拆分的同一会议；
3. Prompt 明确 range 为包含起止端的闭区间，相邻活动不得共享边界片段；
4. `--stop-after-v1` 现在在 V1 结构错误时直接留存已隔离原文并停止，不再自动发起第二次付费 Schema 修复；正常完整生产链路仍保留一次有界修复能力；
5. 付费验收 CLI 的底层暂态网络尝试数固定为 1；超时或 5xx 不会在未再授权的情况下自动发第二/第三个 HTTP 请求。生产默认仍保留 3 次暂态故障尝试；
6. V1 的模型可见 Schema 现在显式约束恰好 7 个场景、最多 128 个省略账本条目、每张卡最多引用 128 个索引单元，并将非用户正文的省略理由收紧到 300 字符；没有为非工作卡设数量配额；
7. 索引请求显式关闭深度思考，统一 V1 请求显式开启；这些状态与 32k/64k 输出预算、300/600 秒超时现在来自同一份请求策略，并纳入固定规则哈希，避免参数改变后误复用旧检查点；
8. 历史真实 V1 原文（84,118 bytes，7 场景、22 卡、22 省略单元）能通过当前 V1 JSON/Schema 解析，最长省略理由仅 27 字符。已有真实 V1 计量证明 64k 预算下 43,210 Token 完整返回、未截断。

当前哈希边界：

- 索引 Prompt SHA-256：`f6c4820aa8cb0009793ac2d7d37861ba2d51069b4944cbce1861713feaef11c9`；
- 全场景 V1 Prompt SHA-256：`2c7902e061605f646febd2f20d808e443486dfa0147d941a136208ba6cbc0f3b`；
- 共享质量契约 SHA-256：`97c0fc57f807959303eed60e1da4ffdb4ca1718fa3048891f479736f9de07cce`；
- 固定规则 SHA-256：`fa6ee17c33f0bfeb5cd20c2cbc7d82e1acbfb32e14fa85f476329887d7ba3cd6`。

最终免费验证证据：

- Beta 8 全相关回归：`403 passed, 1418 deselected`；
- 供应商请求、截断留存与重试策略：`15 passed`；
- 索引单步和 V1 单步 dry-run 均为 `network_accessed=false`、`writes_performed=false`，且 `provider_transient_total_attempts=1`；
- 三组运行时 Prompt 与批准源副本逐字节一致；
- Python 编译检查和 `git diff --check` 通过；
- 完整后端回归在受限沙箱中为 `1788 passed, 28 skipped, 5 failed`。其中 1 项仅因沙箱禁止绑定本地端口，改在允许本地回环端口的环境中单独重验为 `1 passed`；剩余 4 项是仓库已知的 Flash 模型目录与旧“只允许 Pro”测试冲突，与本次索引/V1 修改无关，且按用户要求不处理 Flash。

本轮全面检查、修正和回归没有调用任何付费模型，也没有执行审核、搜索、修订、发布、提交或推送。

## 全链路审查后的 Pro 索引单步验证

用户再次明确授权后，使用审查后的规则执行了一次 `deepseek-v4-pro` 索引请求。本次输入 236,759 Token，输出 5,251 Token，模型耗时 70,033 ms，总耗时 70,235 ms；供应商未返回费用金额。`model_call_count=1`，底层没有自动重试，也没有调用 V1、审核、搜索、修订或发布。

运行目录为 `outputs/beta8-indexed-v1/run-2c5b6376-e12f-4c58-94bd-8c47968ad654`。原始响应为 18,760 bytes，SHA-256 为 `eab1f9e58558db1ab941416a7a6f47f160819212b1a2dea95415d38bb5dd86d6`，已以隔离原文保留。

模型返回 25 个活动会话，但覆盖门禁立即发现 `seg_1_1191` 同时属于“短视频浏览”和“英雄联盟游戏”，因此未保存检查点。离线展开所有区间后还发现：

- `file2_video_browsing_1` 的 `seg_1_1157–1235` 包含了 `file2_lol_game_1` 的 `seg_1_1191–1218`，也包含了另一个视频单元的 `seg_1_1219–1235`；
- 工作沟通候选为 8 个，而不是该样本 Ground Truth 的 5 个；
- 两次车辆电话虽然有两个单元，但 `file2_work_call_3` 的 `seg_1_1272–1314` 完整包含 `file2_work_call_4` 的 `seg_1_1299–1314`，并非两个互斥的真实电话区间；
- 同一场跨文件工作坊仍被拆为 `file2_work_meeting_okr`、`file3_work_meeting_continuation`、`file3_product_share` 和 `file3_product_discussion` 4 个工作单元。

因此这次返回不是“只需修一个重叠片段”，也不能依据 Ground Truth 在本地修成一份冒充模型成功的索引。它证明“自由输出每个 unit 的起止 range”这种数据结构仍允许模型同时产生嵌套区间和重复语义单元；下一步应先改为由有序边界点或互斥时间块构成的索引契约，让重叠在结构上无法表达，再考虑新的付费验证。

## 2026-09-08 两层索引实施完成

已按批准设计完成“互斥覆盖账本 + 可重叠事实目录”的离线实施：

- 模型输出 `primary_sessions`、`embedded_events` 和逐文件 `file_timelines`；每个时间块只输出起点，不再自由填写结束点；
- 本地规范化程序用下一块起点确定性推导上一块终点，因此主覆盖在结构上不能出现空洞或重叠；
- 嵌入事件可在父会话内与主活动或其他嵌入事件共享证据，但越出父会话立即失败；
- 主会话的起止证据必须真实存在、属于该会话；相邻不同对话的证据还必须靠近转换点；
- Runner 先隔离保存模型原文，再解析草稿、规范化、执行评测门禁，最后只保存规范化检查点；失败不会进入 V1，也不会隐式购买索引修复；
- V1 继续接收完整逐字稿。所有主会话和嵌入事件都必须被引用或明确省略，但只有主会话中的真实工作沟通触发“一次沟通一张工作卡”；
- 发布前重新读取规范化检查点并核对工作沟通闭包，避免发布层继续按旧 `activity_sessions[].ranges` 解释数据；
- Ground Truth 只在验收入口于模型生成后注入，支持任意样本数量；生产 Prompt、Schema 和 Runner 默认配置均不知道“5 次”这一答案。

免费回归样本覆盖：游戏主活动与游戏播报/用户评论重叠、相邻两次车辆电话、跨两个文件的一场工作坊、午餐期间的薪资与职业选择闲聊、两段有效活动之间的模型噪声排除。所有样本走同一通用规范化代码，没有按文件名、活动名、真实音频哈希或期望数量写特判。

最新旧付费原文 `run-2c5b6376-e12f-4c58-94bd-8c47968ad654` 已通过只读回归确认无法解析为新 `Beta8EventIndexDraft`。原文继续保留为失败证据，不修改、不迁移，也不冒充可复用检查点。

最终哈希边界：

- 两层索引 Prompt SHA-256：`c173a8fe0f5b7aec0326a15f35628f9ada6592adeb1f8017ea84dd4a7f8b8e97`；
- 全场景 V1 Prompt SHA-256：`a6f5574ca1a1806f8fd7eaa77a05d2e6b80c65916540292b792db2736e2fad13`；
- 共享质量契约 SHA-256：`97c0fc57f807959303eed60e1da4ffdb4ca1718fa3048891f479736f9de07cce`；
- 固定规则 SHA-256：`2179f5292b30c4453cd570ef75740f7a782d970f19b2c1a117a466cfd04134de`；
- 规范化策略版本：`beta8_two_layer_index_normalizer_v1`。

最终离线验证证据：

- 两层索引、V1、评测、Runner 与发布聚焦套件：`305 passed`；
- Beta 8 全相关回归：`446 passed, 1418 deselected`；
- 供应商请求与单次尝试保护：`15 passed`；
- 完整后端回归：`1831 passed, 28 skipped, 5 failed`。其中 4 项仍是既有 Flash 模型目录与旧“只允许 Pro”测试的冲突，1 项仍是受限沙箱禁止绑定本地回环端口；没有新增 Beta 8 失败；
- `--dry-run --stop-after-index` 与 `--dry-run --stop-after-v1` 均为 `network_accessed=false`、`writes_performed=false`、`provider_transient_total_attempts=1`；
- 三份运行时/批准源 Prompt 逐字节一致，Python 编译和 `git diff --check` 通过。

本轮两层索引实施没有调用付费模型、联网搜索、审核、修订或发布，也没有提交、推送、合并或清理工作树。下一次真实 Pro 索引必须在用户阅读本节后重新明确授权。

## 两层索引首次真实 Pro 验证

用户明确授权后，使用 `--stop-after-index` 执行了一次 `deepseek-v4-pro` 索引请求。输入 237,219 Token，输出 2,551 Token，模型耗时 24,793 ms，总耗时 25,003 ms；供应商未返回费用金额。底层请求总数为 1，没有自动重试或索引修复，也没有运行 V1、审核、搜索、修订或发布。

模型返回 14 个主会话、0 个嵌入事件、4 条文件时间线和 14 个 block。草稿 Schema 与本地规范化均通过；主覆盖由 block 起点推导，没有再次出现自由 range 嵌套或重叠。原始响应以 `0600` 权限保存在 `run-6fe817d8-58b3-4459-8593-c54269b8261b/quarantine`，大小 8,659 bytes，SHA-256 为 `cea8a0293fadadb88adc28c3c203ca860a6bda54b5f3630ed44f1cae6762e841`。

Ground Truth 门禁未通过，因此没有保存索引检查点。首次错误文本还暴露了一个本地文件别名问题：规范化索引使用内部 `file_id`，Ground Truth 使用文件名，门禁直接比较二者会把实际重叠报告成 0。该问题已通过新增失败测试后修复，并使用已保存原文免费重放，没有再次调用模型。

修正后的离线匹配表明：前两次工作沟通识别正确；两次车辆电话都匹配到同一个 `sess_1_4`，属于误合并；跨文件工作坊没有任何一个主会话完整覆盖，因为被拆成 `sess_1_5` 与 `sess_2_0`。模型工作主会话总数恰好也是 5，但这是“一次误合并 + 一次误拆分”相互抵消，不是正确的 5 次。

因此，两层契约已验证解决结构重叠与输出膨胀，但尚未解决真实模型的跨文件会话合并和相邻电话拆分。完整记录见 `outputs/beta8-indexed-v1/run-6fe817d8-58b3-4459-8593-c54269b8261b/acceptance-attempt-10-two-layer-pro-index.md`。

基于本次真实证据，索引 Prompt 又增加两项明确的提交前检查：一是检查较长时间跳跃后是否出现前一联系已收束且重新问候/确认身份的新联系；二是逐文件比较首尾表达，若后一文件在语义上直接续接前一文件未完成的回答、论证或行动流程，必须复用同一 `session_id`，不得把文件边界或 `input_start` 当成联系结束/重新开始证据。

该离线修正后的当前哈希为：索引 Prompt `782659a370e9e06b579c9d038df83eb4caa244158f7eb9127865bbf0a358d6fd`，固定规则 `ecdf9fe34e376ba8db9f6e20e6271c78ff760f2a2bd083b5025057ea9b3e77d7`。相关聚焦测试 `235 passed`，Beta 8 全相关回归 `447 passed, 1418 deselected`，索引 dry-run 仍为 `network_accessed=false`、`writes_performed=false`、`provider_transient_total_attempts=1`。修正过程没有新的付费调用。
