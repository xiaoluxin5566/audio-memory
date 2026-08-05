# Audio Memory API Key 配置模块设计

日期：2026-08-05
状态：已冻结，可进入详细设计与编码
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

`SecItemCopyMatching` 的结果必须按状态码区分：

- `errSecSuccess`：条目存在，配置状态为“已配置”。
- `errSecItemNotFound`：条目确实不存在，配置状态为“未配置”。
- `errSecAuthFailed`、`errSecInteractionNotAllowed`、`errSecNotAvailable`、`errSecIO` 等其他错误：配置状态为“未知”，访问状态为“钥匙串不可访问”；不得解释为“未配置”，不得允许覆盖或使用缓存 Key 继续分析。

SQLite 中最近一次状态只可用于辅助提示，不能在 Keychain 不可访问时证明 Key 当前存在。页面应提示用户解锁 Mac 或系统钥匙串后重新校验。

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

SQLite 中的 `validation_status` 是展示缓存，不是配置事实。只有 Keychain 明确返回 `errSecItemNotFound` 时，状态才强制归为“未配置”并忽略 SQLite 旧结果；若 Keychain 返回访问、授权或 I/O 错误，必须进入“钥匙串不可访问”状态。`last_validation_error_message` 只保存归一化后的用户文案，不得保存厂商原始响应片段。

`validation_status`、`last_validated_at`、错误代码和错误文案必须在同一个 SQLite 事务中写入；事务失败则整体回滚。Keychain 和 SQLite 不组成跨存储事务，应用通过固定写入顺序与启动校正实现最终一致。

每次分析结果和意见反馈记录实际使用的 `provider_id`、`model_id`、`model_display_name` 和 Prompt 版本，但不记录 API Key。

### 3.3 临时文件清单

只有在平台实现必须复制或生成临时文件时，SQLite 才写入 `temp_file_manifest`：

- `task_uuid`
- `file_path`
- `created_at`
- `cleanup_status`

`file_path` 必须位于应用专属临时目录，禁止记录或清理用户原始音频路径。创建临时文件前先登记清单，删除物理文件成功后再删除记录；启动时扫描未完成记录，完成清理后移除对应清单。清理前必须解析规范路径并再次确认其位于应用临时目录内。

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
- 当前状态：初始化中、未配置、校验中、可用、不可用、钥匙串不可访问。
- 最近一次成功或失败校验时间。
- “修改配置”按钮。
- “重新校验”按钮。

“修改配置”打开弹窗：

- Kimi、DeepSeek、OpenAI 三个厂商以标签页展示。
- 每个标签页展示与首页一致的全部状态。
- 页面不显示完整或脱敏 Key。
- 已配置厂商明确显示“Key 已安全保存”，输入框保持为空并提示“填写新 Key 可覆盖当前配置”。
- 已配置且可用的厂商可直接选择“设为当前厂商”。
- 未配置厂商填写 Key 并校验成功后，自动保存、设为当前厂商并关闭配置弹窗，不再要求二次确认。
- 不可用厂商需重新校验或填写新 Key，恢复可用后才能设为当前厂商。
- 即使当前厂商不可用，“修改配置”和“重新校验”仍保持可用，用户可以配置其他厂商。
- 钥匙串不可访问时，禁用该厂商的保存、覆盖、切换和分析操作，提示“无法访问系统钥匙串，请解锁 Mac 或钥匙串后重新校验”。
- 限流冷却期间显示“请等待 X 秒后重试”，而不是只将按钮置灰。

切换厂商只影响之后新分析的音频，不修改历史结果。上传队列在开始分析前与厂商无关，切换厂商不会清空或复制队列；点击“开始分析”后才为该批次锁定 `provider_id` 和 `model_id`。

## 6. 校验机制

### 6.1 校验方式

校验必须调用该厂商当前默认分析模型，不得仅依赖模型列表接口。三个适配器使用同一逻辑约束：

- 固定提示为 `Reply exactly: OK`，不包含用户数据。
- `temperature = 0`，禁用流式输出和工具调用。
- 最大输出限制为 4 tokens；若厂商不支持对应参数，由适配器使用最接近的等价配置。
- 单次厂商请求超时为 15 秒，用户主动触发的校验不自动重试。
- 先检查 HTTP 状态、厂商响应结构和业务错误字段；只有不存在错误、能够提取模型正文，且正文去除首尾空白后不区分大小写严格等于 `OK`，才视为校验成功。

错误响应、无法解析的响应、缺少模型输出字段或正文不符合上述协议的响应都不得判定为可用。能够识别厂商业务错误时映射为对应错误；仅输出协议不符时归为 `validation_protocol_error`。

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

所有使用已保存 Key 的校验路径都先读取 Keychain：只有 `errSecSuccess` 才进入网络校验；`errSecItemNotFound` 直接返回“未配置”且不发起网络请求；其他状态码直接返回“钥匙串不可访问”。

第一阶段不增加每 30 分钟等周期性后台校验。开始分析前的真实校验负责发现程序运行期间发生的 Key 失效。

### 6.3 新旧 Key 覆盖规则

- 新 Key校验成功：先使用 `SecItemUpdate` 更新 `kSecValueData`；收到 `errSecItemNotFound` 时执行 `SecItemAdd`；若 Add 因并发返回 `errSecDuplicateItem`，再执行一次 `SecItemUpdate`。禁止使用 Delete+Add 覆盖。
- 上述写入链路中任何其他非 `errSecSuccess` 状态都归一化为 `keychain_unavailable`：不更新 SQLite，不把“候选 Key 校验成功”展示为“配置成功”，旧条目保持原样并向前端返回明确错误。
- 新 Key校验失败：不写入 Keychain，旧 Key继续有效。
- 校验失败后仅在当前弹窗保持打开期间保留候选 Key，按钮文案变为“重新校验”；关闭弹窗立即从前端状态和后端请求上下文中清除候选 Key，再次打开需要重新填写。
- 用户再次修改输入内容后，按钮文案恢复为“保存并校验”。
- 新 Key写入顺序固定为：候选 Key真实校验成功 → Keychain更新成功 → Keychain读取确认 → SQLite事务更新校验元数据。
- 若进程在 Keychain更新前退出，旧 Key保持不变；若在 Keychain更新后、SQLite更新前退出，重启时以 Keychain实际条目重新校验并修正SQLite缓存。

## 7. 状态与交互

每个厂商独立维护以下状态：

- 初始化中：协调器尚未完成首次 Keychain 读取。
- 未配置：Keychain 中没有对应条目。
- 校验中：正在执行最小真实请求，相关操作不可重复触发。
- 可用：最近一次真实请求成功。
- 不可用：最近一次真实请求失败。
- 钥匙串不可访问：无法确定 Key 是否存在，所有依赖 Key 的操作暂停。

启动校验不阻塞用户查看历史、详情或 Prompt 设置。某一厂商校验失败不影响其他厂商。

当前厂商状态与音频操作的关系：

- 可用：允许添加音频和开始分析。
- 未配置或不可用：上传区域置灰，提示“当前模型不可用，请修改配置或重新校验”。
- 已存在上传队列时发生临时校验失败：保留所有文件和进度，只暂停“开始分析”。
- 切换到其他可用厂商后：上传区和分析操作自动恢复。

上传队列生命周期：

- 选择文件时优先保存本地文件引用，不复制音频到临时目录。
- 若平台实现必须生成临时副本，按 SQLite `temp_file_manifest` 管理，在移除文件、取消任务和正常退出时清理；异常退出遗留内容在下次启动时按清单清理。
- 队列在开始分析前不绑定厂商，切换厂商后可继续使用同一批文件。

开始分析时：

1. 等待正在进行的厂商切换完成，原子读取并锁定当前厂商和当前文件列表。
2. 读取当前厂商 Keychain 条目；未配置或钥匙串不可访问时立即返回，不发起网络请求。
3. 对已配置的当前厂商执行最小真实请求。
4. 校验成功后开始本地 Whisper 转写。
5. 校验失败时停留在上传态，保留全部文件，提示修改配置或重新校验。
6. Whisper每完成一个可恢复分段就保存转写进度；完整转写完成后保存可复用的转写产物。
7. 模型分析失败时保留音频、转写和任务上下文，允许使用原厂商重试，或明确切换厂商后复用转写文本重新分析，不重复执行Whisper。

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
| `validation_protocol_error` | 模型校验响应异常，请重新校验 | 是 |
| `keychain_unavailable` | 无法访问系统钥匙串，请解锁 Mac 或检查系统权限 | 是 |
| `unknown` | 校验失败，请重新尝试 | 是 |

`rate_limited` 必须优先遵守厂商返回的 `Retry-After`；冷却期内禁用该厂商的“重新校验”和“开始分析”。没有 `Retry-After` 时采用有限指数退避，不写死固定30秒，不无限自动重试。

已保存 Key 的冷却状态绑定 `provider_id + credential_generation`，而不是永久绑定厂商。候选新 Key使用独立的 `candidate_validation_id` 和临时冷却状态，因此可以立即校验且不改变正式 Key 的generation；旧凭证的冷却记录保留至过期。接口返回 `cooldown_until`，前端按本机当前时间计算并展示剩余秒数，到期后主动刷新状态。

厂商原始错误只允许进入最多50条、支持并发安全写入的纯内存环形诊断缓冲区，进程退出即销毁。写入前必须删除请求头、Key、令牌和其他认证信息；禁止把未过滤原始错误写入文件、标准输出或标准错误。正式日志只记录归一化错误代码。

## 9. 本地接口边界

建议由本地后端提供统一接口，前端不得直接持有或请求 Keychain：

- `GET /api/providers`：读取三个厂商的非敏感状态，包括 `provider_id`、`active`、`state`、`last_validated_at`、归一化错误代码和文案、`cooldown_until`。
- `POST /api/providers/:id/validate`：使用 Keychain 中已有 Key重新校验。
- `PUT /api/providers/:id/key`：校验新 Key，成功后写入 Keychain。
- `POST /api/providers/:id/activate`：将已配置且可用的厂商设为当前厂商。

所有接口响应不得包含 API Key。新 Key只允许出现在 `PUT` 请求体的内存生命周期中，不写入请求日志。

接口中的 `:id` 只接受 `kimi`、`deepseek`、`openai`；`provider:kimi` 等字符串只属于后端 Keychain Account 映射，不允许前端传入。

重复提交相同 Key的最终状态必须幂等，但仍需执行真实校验，因为用户可能正在主动验证同一 Key是否恢复可用。

`GET /api/providers` 优先返回 `ProviderStateCoordinator` 的内存共识；协调器尚未完成初始化时返回 `initializing` 并在后台继续加载，不直接用 SQLite 陈旧缓存生成当前状态。Keychain 读取失败时返回 `keychain_unavailable`。

`POST /api/providers/:id/activate` 只有在目标厂商已配置且可用时成功。接口响应前必须在单个 SQLite 事务内将其他厂商设为非 active、目标厂商设为 active，并同步更新协调器内存状态，确保任意时刻最多一个厂商为 active。

`POST /api/providers/:id/validate`、`PUT /api/providers/:id/key` 以及开始分析前的校验都设置20秒后端总截止时间，包含参数处理、Keychain访问和厂商请求。到期必须取消仍在执行的请求并返回 `timeout`，不得让前端无限等待。

## 10. 状态协调器与并发一致性

本地后端提供单例 `ProviderStateCoordinator`，统一协调：

- `KeychainRepository`：读取、添加和更新 Key，是配置事实来源。
- `ProviderValidationService`：执行真实校验、任务去重、冷却和结果归一化。
- `ProviderMetadataRepository`：在 SQLite 中保存非敏感展示缓存。
- `AnalysisJobCoordinator`：锁定批次、保存转写恢复点和恢复模型分析。

本地后端使用 macOS 内核管理的 `flock` 或 `fcntl` 排他锁，并在进程整个生命周期内持有锁文件描述符。正常退出、崩溃或 `kill -9` 后由内核自动释放锁，不使用“锁文件年龄超过阈值”作为强制接管条件。锁文件中的 PID 和启动时间只用于诊断。

新进程获取锁失败时先检查已有本地服务健康状态：服务正常则提示“服务已运行”并退出；服务异常则返回可诊断的启动错误，不绕过内核锁并发启动。Keychain 的重复条目重试和 SQLite 事务仍作为跨进程防御措施。

### 10.1 credential generation

- 每个厂商在进程内维护单调递增的 `credential_generation`。
- 启动校验、手动校验和开始分析前校验使用当前generation。
- 候选新 Key校验使用独立的 `candidate_validation_id`，不修改正式厂商状态，也不使正式 Key 的校验任务失效。
- 只有候选 Key校验成功且 Keychain 写入确认成功后，才递增 `credential_generation`，使旧 Key相关任务失效并更新正式厂商状态。
- 每个任务完成时必须携带启动时generation；仅当其等于当前generation时，才允许更新状态和SQLite元数据。
- 旧任务无法可靠取消时可以继续运行，但结果必须被丢弃。

### 10.2 in-flight任务去重

- 同一厂商、同一generation同一时间只创建一个in-flight校验任务。
- 启动校验进行中时用户点击“开始分析”，分析流程等待并复用现有任务结果，不创建第二个请求。
- UI进入“正在校验模型”状态并禁用重复操作。
- 候选新 Key按 `candidate_validation_id` 去重，不复用正式 Key校验任务，其“校验中”状态只显示在配置弹窗内。
- 用户在候选 Key 尚未持久化时关闭弹窗，后端取消对应 HTTP 请求并清除候选状态；无法取消的响应因 `candidate_validation_id` 已失效，不得写入正式状态或Keychain。
- 由于候选流程从未修改正式厂商状态，关闭弹窗后无需快照回滚，首页继续显示正式 Key 原有状态。

### 10.3 Keychain与SQLite协调

- 同一厂商的 Keychain 写入在协调器内串行执行，避免本进程内多个候选 Key 交错覆盖。
- Keychain条目存在性决定“未配置/已配置”；SQLite不得反向创建或删除Keychain条目。
- SQLite校验元数据在单个数据库事务中更新，但不与Keychain组成跨存储事务。
- 启动时执行状态校正：Keychain明确返回无条目才强制未配置；Keychain有条目则重新校验并覆盖SQLite缓存；Keychain不可访问则暂停校正并保留“未知”配置状态。
- `SecItemUpdate`或回退写入失败时旧Key保持原样，不更新SQLite。
- Keychain更新成功但SQLite失败时不回滚Keychain；当前状态标记为需要校正，并在本次运行重试元数据写入或下次启动重新生成。

### 10.4 分析批次锁定

- 厂商激活和分析批次锁定通过协调器中的全局串行临界区执行；分析请求必须等待正在进行的activate完成。
- activate接口在SQLite事务提交成功后、仍持有协调器锁时同步更新内存状态并返回；数据库事务失败则保持原active厂商并返回失败。
- 分析请求只在短临界区内读取并锁定厂商、模型和文件快照，随后立即释放全局锁；最长15秒的网络校验在锁外执行，不阻塞厂商配置和其他无关操作。
- 分析前校验仍受20秒接口总截止时间约束；校验失败或超时不启动Whisper。
- 开始分析前的上传队列是厂商无关数据。
- 开始分析校验成功后锁定该批次的`provider_id`、`model_id`和Prompt版本。
- 中途切换全局当前厂商不影响已锁定批次。
- 模型阶段失败后，用户主动选择“更换厂商重新分析”会创建新的分析尝试，复用原音频与转写，记录新的厂商和模型快照。

## 11. 验收与测试

### 11.1 功能验收

- 三个厂商可以分别配置、长期保存和校验。
- 关闭并重新启动程序后，Key仍可用且页面不显示 Key内容。
- 启动后只校验已配置厂商，并独立展示结果。
- Keychain 不可访问时不误显示“未配置”，也不允许保存、切换或分析。
- 可用厂商之间无需重新填写 Key即可切换。
- 配置新厂商成功后自动切换为当前厂商并关闭配置弹窗；切换至其他已配置厂商时仍由用户明确操作。
- 新 Key失败不会覆盖旧 Key。
- 开始分析前校验失败时，文件列表完整保留且 Whisper 不启动。
- 未配置厂商点击开始分析时不发起厂商网络请求。
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
- `SecItemAdd`遭遇`errSecDuplicateItem`后回退`SecItemUpdate`的并发测试。
- Keychain读取或写入返回授权、交互、I/O错误时的状态和旧Key保留测试。
- `credential_generation`旧任务结果丢弃测试。
- 候选 Key校验不改变正式状态、关闭弹窗取消候选请求且旧 Key状态保持不变的测试。
- 启动并行校验、同厂商任务去重与开始分析等待复用测试。
- Keychain更新成功、SQLite失败后的启动校正测试。
- 开始分析前校验失败不启动 Whisper 的流程测试。
- 模型调用失败保留转写、原厂商重试和更换厂商复用转写测试。
- 临时文件正常退出清理和异常退出后启动清理测试。
- `temp_file_manifest`仅清理应用临时目录、不删除用户原文件的安全测试。
- activate与开始分析并发时使用新active厂商的互斥测试，以及active唯一性事务测试。
- 冷却期状态透传、前端倒计时和新credential generation独立校验测试。
- 校验响应仅在去除首尾空白且忽略大小写后等于`OK`时成功；引号、附加文字、错误或不可解析响应均不误判的测试。
- 厂商请求15秒超时、接口20秒总截止和超时后不启动Whisper的测试。
- 正常退出、崩溃和`kill -9`后内核锁释放，以及存活实例持锁时拒绝第二实例的测试。
- 前端全部状态、厂商切换、按钮禁用和错误文案测试。

## 12. 已确认决策

- 三个厂商 Key分别存入 macOS Keychain。
- 页面不展示完整或脱敏 Key。
- 使用默认分析模型的极小真实请求进行校验。
- 启动、保存、修改、手动重试和开始分析前均触发校验。
- 启动时校验所有已配置厂商。
- 第一阶段不开放模型选择，不支持删除 Key。
- 当前厂商不可用时不允许新增音频，但保留已有上传队列。
- 厂商之间手动切换，不做自动降级。
- 配置新厂商成功后自动设为当前厂商并关闭配置弹窗。
- 上传队列在开始分析前与厂商无关，切换厂商不清空队列。
- 模型分析失败时保留Whisper转写，允许重试或更换厂商复用。
- Keychain是配置事实来源，SQLite只保存非敏感校验缓存。
- 候选 Key状态与正式 Key状态隔离，只有持久化成功才更新credential generation。
- 单实例依赖内核排他锁自动释放，不使用锁文件超时接管。

## 13. 已解决的边界风险

- Keychain 状态码只有 `errSecItemNotFound` 代表未配置；访问失败使用独立状态。
- Keychain 并发新增通过 Add 返回重复后再次 Update 收敛。
- 限流状态按凭证generation隔离，新 Key可以立即进行一次真实校验。
- 临时文件通过 SQLite 清单恢复清理，且只处理应用专属临时目录。
- provider接口只返回协调器内存共识，不把SQLite缓存当作当前事实。
- 厂商激活与分析批次锁定串行执行，active更新满足唯一性。
- 极小请求必须满足规范化后的`OK`响应协议，避免任意非空正文造成假阳性。
- 网络校验在全局锁外执行，并受15秒请求超时和20秒接口总截止限制。

## 14. 技术依据

- Apple建议更新已有Keychain条目时使用`SecItemUpdate`，避免重复添加或遗留旧条目：<https://developer.apple.com/documentation/security/updating-and-deleting-keychain-items>
- Apple建议在macOS上通过`kSecUseDataProtectionKeychain`使用数据保护钥匙串：<https://developer.apple.com/documentation/security/ksecusedataprotectionkeychain>
- Apple要求选择满足使用场景的最严格Keychain可访问级别：<https://developer.apple.com/documentation/security/restricting-keychain-item-accessibility>
- Apple将`errSecDuplicateItem`定义为相同主键的条目已存在：<https://developer.apple.com/documentation/security/errsecduplicateitem>
- Apple Security Framework状态码用于区分条目不存在、认证失败、交互受限和I/O错误：<https://developer.apple.com/documentation/security/security-framework-result-codes>
- SQLite事务只能保证SQLite数据库内部修改的原子性；Keychain不属于SQLite事务边界：<https://www.sqlite.org/lang_transaction.html>
