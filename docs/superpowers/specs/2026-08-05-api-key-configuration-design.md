# Audio Memory API Key 配置模块设计

日期：2026-08-05
状态：外部评审修订版，待用户最终审阅
适用范围：macOS 第一阶段体验版

## 1. 目标与边界

本模块负责 Kimi、DeepSeek、OpenAI 三个分析模型厂商的 API Key 配置、长期本地保存、可用性校验和当前厂商切换。

第一阶段目标：

- 用户不需要账号体系即可在本机完成模型配置。
- 三个厂商的 Key 相互独立，可分别配置和校验。
- 在音频进入耗时的本地转写流程前发现 Key、权限或网络问题。
- Key 永远不写入普通文件、SQLite、日志、分析记录或意见反馈记录。

第一阶段不包含：

- 删除已保存的 API Key。
- 在页面中展示完整或脱敏 Key。
- 由用户选择具体模型版本。
- 自动切换厂商或降级到其他厂商。
- 后台定时轮询校验 Key。

## 2. 方案选择

采用“macOS Keychain + 多节点真实请求校验”。

未采用的方案：

- 仅保存时校验：无法在 Key 后续失效时提前发现问题。
- 普通本地配置文件：存在明文泄露、日志误写和备份传播风险。

该方案会产生少量额外模型请求，但每次校验都使用极小请求，优先保证完整体验和故障前置发现。

## 3. 数据与存储

### 3.1 Keychain

三个厂商分别使用独立 Keychain 条目：

- Service：`Audio Memory`
- Account：`provider:kimi`
- Account：`provider:deepseek`
- Account：`provider:openai`

Keychain 只保存 API Key 原文。应用不得把 Key 复制到其他持久化介质。

Keychain 是“厂商是否已配置”以及“当前 Key 内容”的唯一事实来源。应用每次读取厂商状态时都以 `SecItemCopyMatching` 的实际结果为准，不在 SQLite 中保存第二份 `configured` 标志。

Keychain 条目使用：

- `kSecUseDataProtectionKeychain = true`
- `kSecAttrAccessible = kSecAttrAccessibleWhenUnlockedThisDeviceOnly`
- `kSecAttrSynchronizable = false`

第一阶段产品只在用户已解锁 Mac 并主动启动程序时运行，不需要锁屏后台访问，也不允许通过 iCloud 同步到其他设备。

### 3.2 SQLite 配置元数据

SQLite 只保存非敏感状态：

- `provider_id`
- `active`
- `validation_status`
- `last_validated_at`
- `last_validation_error_code`
- `last_validation_error_message`
- `default_model_id`

SQLite 中的 `validation_status` 是展示缓存，不是配置事实。若 Keychain 读取不到条目，状态必须强制归为“未配置”，忽略 SQLite 中的旧校验结果。

`validation_status`、`last_validated_at`、错误代码和错误文案必须在同一个 SQLite 事务中写入；事务失败则整体回滚。Keychain 和 SQLite 不组成跨存储事务，应用通过固定写入顺序与启动校正实现最终一致。

每次分析结果和意见反馈记录实际使用的 `provider_id`、`model_id`、`model_display_name` 和 Prompt 版本，但不记录 API Key。

## 4. 默认模型

- Kimi、DeepSeek、OpenAI 各内置一个默认分析模型。
- 默认模型名称由系统配置维护，不写入场景 Prompt。
- 第一阶段页面不提供模型下拉框。
- 后续更换默认模型只修改系统配置，不改变页面和业务数据结构。
- 历史记录中的模型信息是生成当时的不可变快照，不随默认模型配置变更，也不进行迁移；需要展示时标注“当时使用的模型”。

具体模型 ID 在实现计划中依据开发时各厂商官方 API 文档确定，并通过配置文件管理。

## 5. 页面结构

首页左侧“模型与 API Key”卡片展示：

- 当前厂商名称。
- 当前状态：未配置、校验中、可用、不可用。
- 最近一次成功或失败校验时间。
- “修改配置”按钮。
- “重新校验”按钮。

“修改配置”打开弹窗：

- Kimi、DeepSeek、OpenAI 三个厂商以标签页展示。
- 每个标签页展示未配置、可用或不可用状态。
- 页面不显示完整或脱敏 Key。
- 已配置厂商明确显示“Key 已安全保存”，输入框保持为空并提示“填写新 Key 可覆盖当前配置”。
- 已配置且可用的厂商可直接选择“设为当前厂商”。
- 未配置厂商填写 Key并校验成功后只保存配置，不自动切换；用户明确点击“设为当前厂商”后才切换。
- 不可用厂商需重新校验或填写新 Key，恢复可用后才能设为当前厂商。
- 即使当前厂商不可用，“修改配置”和“重新校验”仍保持可用，用户可以配置其他厂商。

切换厂商只影响之后新分析的音频，不修改历史结果。上传队列在开始分析前与厂商无关，切换厂商不会清空或复制队列；点击“开始分析”后才为该批次锁定 `provider_id` 和 `model_id`。

## 6. 校验机制

### 6.1 校验方式

校验必须调用该厂商当前默认分析模型，发送极小真实请求并要求只返回 `OK`。不得仅依赖模型列表接口。

一次成功校验同时证明：

- Key 可以通过身份验证。
- 账户具有默认模型访问权限。
- 当前网络能够访问厂商服务。
- 默认模型可以完成最小生成请求。

### 6.2 触发时机

以下情况必须校验：

1. 新增 API Key。
2. 修改已有 API Key。
3. 用户点击“重新校验”。
4. 用户点击“开始分析”。
5. 每次启动本地后端进程。

本地后端启动后立即读取 Keychain，并行校验全部已配置厂商；页面只订阅校验状态，不负责触发启动校验。页面路由切换和刷新不重复创建启动校验任务。

第一阶段不增加每 30 分钟等周期性后台校验。开始分析前的真实校验负责发现程序运行期间发生的 Key 失效。

### 6.3 新旧 Key 覆盖规则

- 新 Key校验成功：已有条目使用 `SecItemUpdate` 更新 `kSecValueData`；只有收到 `errSecItemNotFound` 时才使用 `SecItemAdd`。禁止使用 Delete+Add 覆盖。
- 新 Key校验失败：不写入 Keychain，旧 Key继续有效。
- 校验失败后仅在当前弹窗保持打开期间保留候选 Key，按钮文案变为“重新校验”；关闭弹窗立即从前端状态和后端请求上下文中清除候选 Key，再次打开需要重新填写。
- 用户再次修改输入内容后，按钮文案恢复为“保存并校验”。
- 新 Key写入顺序固定为：候选 Key真实校验成功 → Keychain更新成功 → Keychain读取确认 → SQLite事务更新校验元数据。
- 若进程在 Keychain更新前退出，旧 Key保持不变；若在 Keychain更新后、SQLite更新前退出，重启时以 Keychain实际条目重新校验并修正SQLite缓存。

## 7. 状态与交互

每个厂商独立维护四种状态：

- 未配置：Keychain 中没有对应条目。
- 校验中：正在执行最小真实请求，相关操作不可重复触发。
- 可用：最近一次真实请求成功。
- 不可用：最近一次真实请求失败。

启动校验不阻塞用户查看历史、详情或 Prompt 设置。某一厂商校验失败不影响其他厂商。

当前厂商状态与音频操作的关系：

- 可用：允许添加音频和开始分析。
- 未配置或不可用：上传区域置灰，提示“当前模型不可用，请修改配置或重新校验”。
- 已存在上传队列时发生临时校验失败：保留所有文件和进度，只暂停“开始分析”。
- 切换到其他可用厂商后：上传区和分析操作自动恢复。

上传队列生命周期：

- 选择文件时优先保存本地文件引用，不复制音频到临时目录。
- 若平台实现必须生成临时副本，在移除文件、取消任务和正常退出时清理；异常退出遗留内容在下次启动时按任务清单清理。
- 队列在开始分析前不绑定厂商，切换厂商后可继续使用同一批文件。

开始分析时：

1. 锁定当前厂商和当前文件列表。
2. 对当前厂商执行最小真实请求。
3. 校验成功后开始本地 Whisper 转写。
4. 校验失败时停留在上传态，保留全部文件，提示修改配置或重新校验。
5. Whisper每完成一个可恢复分段就保存转写进度；完整转写完成后保存可复用的转写产物。
6. 模型分析失败时保留音频、转写和任务上下文，允许使用原厂商重试，或明确切换厂商后复用转写文本重新分析，不重复执行Whisper。

## 8. 错误分类

应用将厂商返回错误归一化为：

| 错误代码 | 用户文案 | 是否可重试 |
|---|---|---|
| `invalid_key` | API Key 无效，请重新填写 | 否，需修改 Key |
| `permission_denied` | 当前账户无模型访问权限 | 否，需处理账户权限 |
| `insufficient_balance` | 当前账户余额不足 | 否，需处理账户余额 |
| `rate_limited` | 请求过于频繁，请稍后重试 | 是 |
| `network_error` | 网络连接失败，请检查网络后重试 | 是 |
| `provider_unavailable` | 模型服务暂时不可用，请稍后重试 | 是 |
| `timeout` | 校验超时，请重新校验 | 是 |
| `keychain_unavailable` | 无法访问系统钥匙串，请解锁 Mac 或检查系统权限 | 是 |
| `unknown` | 校验失败，请重新尝试 | 是 |

`rate_limited` 必须优先遵守厂商返回的 `Retry-After`；冷却期内禁用该厂商的“重新校验”和“开始分析”。没有 `Retry-After` 时采用有限指数退避，不写死固定30秒，不无限自动重试。

厂商原始错误只允许进入最多50条的纯内存环形诊断缓冲区，进程退出即销毁。写入前必须删除请求头、Key、令牌和其他认证信息；禁止把未过滤原始错误写入文件、标准输出或标准错误。正式日志只记录归一化错误代码。

## 9. 本地接口边界

建议由本地后端提供统一接口，前端不得直接持有或请求 Keychain：

- `GET /api/providers`：读取三个厂商的非敏感状态。
- `POST /api/providers/:id/validate`：使用 Keychain 中已有 Key重新校验。
- `PUT /api/providers/:id/key`：校验新 Key，成功后写入 Keychain。
- `POST /api/providers/:id/activate`：将已配置且可用的厂商设为当前厂商。

所有接口响应不得包含 API Key。新 Key只允许出现在 `PUT` 请求体的内存生命周期中，不写入请求日志。

接口中的 `:id` 只接受 `kimi`、`deepseek`、`openai`；`provider:kimi` 等字符串只属于后端 Keychain Account 映射，不允许前端传入。

重复提交相同 Key的最终状态必须幂等，但仍需执行真实校验，因为用户可能正在主动验证同一 Key是否恢复可用。

## 10. 状态协调器与并发一致性

本地后端提供单例 `ProviderStateCoordinator`，统一协调：

- `KeychainRepository`：读取、添加和更新 Key，是配置事实来源。
- `ProviderValidationService`：执行真实校验、任务去重、冷却和结果归一化。
- `ProviderMetadataRepository`：在 SQLite 中保存非敏感展示缓存。
- `AnalysisJobCoordinator`：锁定批次、保存转写恢复点和恢复模型分析。

### 10.1 credential generation

- 每个厂商在进程内维护单调递增的 `credential_generation`。
- 启动校验、手动校验和开始分析前校验使用当前generation。
- 用户提交候选新 Key时立即增加generation，使旧 Key相关任务失效。
- 每个任务完成时必须携带启动时generation；仅当其等于当前generation时，才允许更新状态和SQLite元数据。
- 旧任务无法可靠取消时可以继续运行，但结果必须被丢弃。

### 10.2 in-flight任务去重

- 同一厂商、同一generation同一时间只创建一个in-flight校验任务。
- 启动校验进行中时用户点击“开始分析”，分析流程等待并复用现有任务结果，不创建第二个请求。
- UI进入“正在校验模型”状态并禁用重复操作。
- 候选新 Key属于新的generation，不复用旧 Key校验任务。

### 10.3 Keychain与SQLite协调

- Keychain条目存在性决定“未配置/已配置”；SQLite不得反向创建或删除Keychain条目。
- SQLite校验元数据在单个数据库事务中更新，但不与Keychain组成跨存储事务。
- 启动时执行状态校正：Keychain无条目则强制未配置；Keychain有条目则重新校验并覆盖SQLite缓存。
- `SecItemUpdate`失败时旧Key保持原样，不更新SQLite。
- Keychain更新成功但SQLite失败时不回滚Keychain；当前状态标记为需要校正，并在本次运行重试元数据写入或下次启动重新生成。

### 10.4 分析批次锁定

- 开始分析前的上传队列是厂商无关数据。
- 开始分析校验成功后锁定该批次的`provider_id`、`model_id`和Prompt版本。
- 中途切换全局当前厂商不影响已锁定批次。
- 模型阶段失败后，用户主动选择“更换厂商重新分析”会创建新的分析尝试，复用原音频与转写，记录新的厂商和模型快照。

## 11. 验收与测试

### 11.1 功能验收

- 三个厂商可以分别配置、长期保存和校验。
- 关闭并重新启动程序后，Key仍可用且页面不显示 Key内容。
- 启动后只校验已配置厂商，并独立展示结果。
- 可用厂商之间无需重新填写 Key即可切换。
- 配置新厂商成功后不自动切换，只有用户明确操作才变更当前厂商。
- 新 Key失败不会覆盖旧 Key。
- 开始分析前校验失败时，文件列表完整保留且 Whisper 不启动。
- 当前厂商不可用时，历史和 Prompt 设置仍可操作。
- 当前厂商不可用时，修改配置和重新校验仍可操作。
- 模型分析失败后可复用完整转写重试或更换厂商，不重复执行 Whisper。

### 11.2 安全验收

- SQLite、普通配置文件、正式日志和意见反馈目录中均不存在 API Key。
- 所有 API 响应均不存在完整或脱敏 Key。
- 诊断错误中不包含认证请求头或令牌。
- 清除历史不会删除 Keychain 中的 API Key。
- Keychain条目使用Data Protection Keychain、`WhenUnlockedThisDeviceOnly`且不参与iCloud同步。
- 候选新Key在关闭配置弹窗后不再保留于内存。

### 11.3 自动化测试

- Keychain 读写和覆盖的集成测试。
- 三个厂商适配器的成功、认证失败、权限失败、余额不足、限流、网络失败和超时测试。
- 新 Key失败保留旧 Key的原子性测试。
- `credential_generation`旧任务结果丢弃测试。
- 启动并行校验、同厂商任务去重与开始分析等待复用测试。
- Keychain更新成功、SQLite失败后的启动校正测试。
- 开始分析前校验失败不启动 Whisper 的流程测试。
- 模型调用失败保留转写、原厂商重试和更换厂商复用转写测试。
- 临时文件正常退出清理和异常退出后启动清理测试。
- 前端四种状态、厂商切换、按钮禁用和错误文案测试。

## 12. 已确认决策

- 三个厂商 Key分别存入 macOS Keychain。
- 页面不展示完整或脱敏 Key。
- 使用默认分析模型的极小真实请求进行校验。
- 启动、保存、修改、手动重试和开始分析前均触发校验。
- 启动时校验所有已配置厂商。
- 第一阶段不开放模型选择，不支持删除 Key。
- 当前厂商不可用时不允许新增音频，但保留已有上传队列。
- 厂商之间手动切换，不做自动降级。
- 配置新厂商成功后不自动切换，必须由用户明确选择。
- 上传队列在开始分析前与厂商无关，切换厂商不清空队列。
- 模型分析失败时保留Whisper转写，允许重试或更换厂商复用。
- Keychain是配置事实来源，SQLite只保存非敏感校验缓存。

## 13. 技术依据

- Apple建议更新已有Keychain条目时使用`SecItemUpdate`，避免重复添加或遗留旧条目：<https://developer.apple.com/documentation/security/updating-and-deleting-keychain-items>
- Apple建议在macOS上通过`kSecUseDataProtectionKeychain`使用数据保护钥匙串：<https://developer.apple.com/documentation/security/ksecusedataprotectionkeychain>
- Apple要求选择满足使用场景的最严格Keychain可访问级别：<https://developer.apple.com/documentation/security/restricting-keychain-item-accessibility>
- SQLite事务只能保证SQLite数据库内部修改的原子性；Keychain不属于SQLite事务边界：<https://www.sqlite.org/lang_transaction.html>
