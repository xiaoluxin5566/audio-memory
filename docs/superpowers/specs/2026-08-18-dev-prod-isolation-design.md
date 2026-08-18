# Audio Memory 开发环境 / 正式环境隔离设计

日期：2026-08-18
状态：待用户书面审阅
基线：`0f7df7e`（已发布 `v0.1.0-beta.1` 的终端验证提交）
候选分支：`codex/dev-prod-isolation`

## 1. 目标

在同一台 Mac 上建立两个可同时存在、可明确辨认、默认不会互相污染的 Audio Memory 运行环境：

- 正式环境继续承载真实音频、历史数据库、报告和正式 provider 配置。
- 开发环境承载测试音频、测试数据库、测试报告和独立 provider 配置。
- 正式环境的现有路径、历史数据库、LaunchAgent 和 `v0.1.0-beta.1` Release 保持不变。
- 环境隔离必须由后端实际执行，而不只依赖启动脚本或界面文案。

第一期只建立运行、数据、凭据和识别边界，不改变转写、报告生成、审计或发布内容逻辑。

## 2. 非目标

本期不包含：

- 自动更新或环境间的数据同步。
- 正式数据库迁移、改名、搬移或重建。
- 恢复报告反馈入口或继续问答入口。
- 修改报告生成、模型选择或 Prompt 逻辑。
- 创建新的开发 LaunchAgent。
- 读取现有 Keychain 密钥明文、调用真实 provider 或使用真实音频验收。
- 合并到 `main`、推送远端、创建标签或发布 `v0.1.0-beta.2`。

## 3. 方案选择

采用显式 Profile 加受控覆盖变量的方案。`production` 和 `development` 是唯一合法 profile；任何其他值都应在创建应用或启动服务前失败，而不能静默回退。

不采用“只设置 `AUDIO_MEMORY_DATA_ROOT`”的方案，因为当前后端不会读取该变量，而且脚本各自拼接路径容易产生不一致。不采用两套完全独立安装，因为大模型和运行依赖会重复，占用空间且增加维护成本。

## 4. 统一运行配置

后端新增不可变的统一运行配置对象，负责一次性解析下列值：

| 配置 | production 默认值 | development 默认值 |
|---|---|---|
| `AUDIO_MEMORY_PROFILE` | `production` | 由开发入口显式设置为 `development` |
| `AUDIO_MEMORY_DATA_ROOT` | `~/Library/Application Support/AudioMemory` | 当前隔离工作树的 `.runtime/dev` |
| `AUDIO_MEMORY_MODEL_ROOT` | `<data-root>/models` | 正式环境的 `~/Library/Application Support/AudioMemory/models` |
| `AUDIO_MEMORY_KEYCHAIN_SERVICE` | `Audio Memory` | `Audio Memory Dev` |
| `AUDIO_MEMORY_PORT` | `8765` | `8766` |

解析规则：

1. `AUDIO_MEMORY_PROFILE` 缺失时默认为 `production`，保证现有安装包和 CLI 行为不变。
2. 明确设置的覆盖变量优先于 profile 默认值，但必须经过类型、范围和安全校验。
3. 路径展开 `~`、转为绝对路径并解析已有软链接后再参与比较。
4. 端口必须是 `1..65535` 的整数。
5. Keychain service 必须是去除首尾空白后仍非空的字符串。
6. 解析后的配置由 `create_app()`、`AppPaths`、Keychain、健康接口和启动脚本共同使用，避免重复推导。
7. 测试仍可向 `create_app()` 显式注入路径和端口；显式注入优先于环境解析，保证单元测试可控。

## 5. 数据和路径边界

`AppPaths` 将数据根目录和模型根目录分开接收。除 `models` 外，所有可写资源都必须位于当前 profile 的数据根目录：

- `audio-memory.sqlite3`
- `runtime/`、锁文件、本地会话安全数据库和日志
- `staging/`
- `audio/`
- `prompts/`
- `意见反馈/`

`development` 的默认数据根目录为隔离工作树自身的 `.runtime/dev`。它不能读取或写入正式数据库、正式音频、正式 Prompt、正式反馈、正式日志、正式 staging 或正式锁文件。

开发启动保护必须拒绝以下情况：

- 开发数据根目录与正式数据根目录相同。
- 开发数据根目录位于正式数据根目录内部。
- 开发数据根目录通过已有软链接解析后落入上述两种情况。
- 开发模型根目录不是正式模型目录，但与开发数据根目录之外的其他受保护正式可写目录重叠。

保护判断必须发生在创建目录、运行迁移或启动 Uvicorn 之前。拒绝时输出清楚的中文错误，且不得创建数据库、锁或日志。

正式环境不新增数据迁移：默认数据库路径继续精确为 `~/Library/Application Support/AudioMemory/audio-memory.sqlite3`。

## 6. 模型共享边界

开发环境默认共享正式环境的模型目录，以避免重复下载 Whisper 和说话人分段模型。该共享是只读契约：

- 开发启动流程只检查模型是否存在和是否可读。
- 开发流程不得下载、更新、删除、移动或修复正式模型文件和 manifest。
- `AppPaths.ensure_directories()` 不得对外部共享模型目录执行创建或 `chmod`。
- 若共享模型缺失，开发启动或诊断应明确报告缺失，不得自动写入正式模型目录。
- 如需测试模型下载或修复，必须显式把 `AUDIO_MEMORY_MODEL_ROOT` 指向开发数据目录内的独立模型目录。

正式环境保持现有模型写入能力和路径，不受上述开发只读限制影响。

## 7. Keychain 隔离

`KeychainRepository` 不再使用只能硬编码的 service，而是通过构造参数接收 service；默认参数仍为 `Audio Memory`，维持现有调用兼容性。

- production 使用 `Audio Memory`。
- development 使用 `Audio Memory Dev`。
- provider account 名称保持现有 `provider:<id>` 规则。
- 健康接口、日志和错误信息不得返回密钥内容。

自动化测试使用假的 Security client 验证 service 参数，不调用 macOS Keychain，也不读取现有凭据。

## 8. 启动和停止流程

### 8.1 正式环境

现有全局入口继续为：

```text
audio-memory start|stop|restart|status|doctor|logs|version
```

其默认 profile、端口、数据路径、LaunchAgent label 和安装位置保持不变。安装器不得迁移或清空历史数据库；升级验证仍要求先生成数据库备份。

### 8.2 开发环境

新增：

```text
./scripts/dev-start.sh
./scripts/dev-stop.sh
```

`dev-start.sh` 固定设置 `development` profile、默认 `8766`、`.runtime/dev` 数据目录、`Audio Memory Dev` Keychain service 和正式模型目录，然后在前台启动后端。它不注册 LaunchAgent。开发前端代理默认指向 `http://127.0.0.1:8766`。

开发 PID、锁和日志只写入 `.runtime/dev/runtime`。`dev-stop.sh` 只根据开发环境的 PID/健康信息停止开发实例；不得调用正式 LaunchAgent 的 `bootout`。陈旧 PID 必须先验证对应进程和开发端口，不能盲目杀进程。

同一环境重复启动时给出“已在运行”；端口被其他程序占用时失败并明确提示，不能自动换端口导致 UI 连错后端。

## 9. 可观察性和界面标识

`GET /api/health` 增加：

```json
{
  "status": "ok",
  "profile": "development",
  "version": "...",
  "platform": "macOS",
  "architecture": "arm64"
}
```

健康接口不返回数据目录、模型目录、Keychain service 或任何凭据。

前端从健康接口获取 profile。仅在 `development` 时显示持续可见但轻量的“开发环境”标识；production 不新增标识。标识不改变上传、Feed、报告详情或设置页的交互结构。

开发前端若连接到返回 `production` 的后端，应显示阻断性错误并禁止写操作，防止代理变量误指向正式服务。只依据构建模式显示标识是不够的，运行时健康信息才是判断依据。

## 10. Doctor 行为

`scripts/doctor.sh` 使用与后端一致的 profile、数据根目录、模型根目录和端口输入，不再把正式数据目录写死在各项检查里。

诊断输出显示 profile、端口和“正式/开发数据目录”类别；为避免泄露用户目录结构，常规输出不打印未经脱敏的完整绝对路径。它分别检查：

- 数据目录是否可写。
- 模型目录是否可读；development 共享正式模型时不检查可写性。
- 当前 profile 对应的数据库迁移和恢复状态。
- 当前 profile 对应端口的健康结果及返回 profile 是否匹配。
- production 才检查正式 LaunchAgent；development 不检查也不创建 LaunchAgent。

## 11. 失败处理

- 非法 profile、端口、空 Keychain service 或危险路径：启动前失败，退出码非零。
- 健康接口返回的 profile 与期望不一致：CLI/开发代理视为错误，不把另一个环境认作“已在运行”。
- 开发共享模型缺失：给出只读缺失提示，不自动下载到正式目录。
- 数据目录不可创建或不可写：迁移前失败。
- 开发停止信息过期：清理开发 PID 记录并报告实例未运行，不影响其他进程。

错误信息不得包含密钥、provider 响应正文或未经必要裁剪的绝对敏感路径。

## 12. 测试设计

所有行为变更遵循测试先行：先写能因缺少功能而失败的测试，确认失败原因，再写最小实现。

### 12.1 后端单元与集成测试

- profile 默认值保持 production。
- development 的五项默认值正确。
- 每个覆盖变量生效；非法值在应用创建前失败。
- `AppPaths` 将模型根目录与可写数据根目录分离。
- development 共享模型时不创建、不 chmod 模型目录。
- `create_app()` 使用统一配置，并在健康接口暴露 profile。
- production 默认数据库路径逐字符保持原值。
- 两个 profile 的数据库、锁、本地会话安全、staging、audio、prompts、feedback 和 runtime 不重叠。
- Keychain repository 使用传入 service，production 默认兼容，development 不触碰 production service。

### 12.2 脚本测试

- `dev-start.sh` 生成正确的 profile、端口、数据和模型配置。
- 相同、子目录和软链接三种正式数据污染路径均被拒绝，且拒绝前没有创建文件。
- 开发启动/停止不调用正式 LaunchAgent。
- 健康检查必须同时匹配端口和 profile。
- `doctor.sh` 分别诊断 production 与 development。
- 正式 CLI 的 `start/status/stop/version/doctor/logs` 默认行为不变。
- 安装器仍备份并保留原数据库，Release 白名单不包含 `.runtime`、数据库、音频、日志、模型或环境文件。

### 12.3 前端测试

- development 健康响应显示明确环境标识。
- production 不显示开发标识。
- 开发构建连接到 production 后端时阻止写操作。
- Vite 开发代理默认指向 `8766`，并继续满足 loopback 与同源写请求保护。
- 完整前端单元测试和生产构建通过。

### 12.4 隔离验收

使用临时目录和假 Keychain 完成自动化验收，不使用正式数据库或真实音频：

1. 分别启动 production fixture 和 development fixture。
2. 验证 `8765` 与 `8766` 各自返回匹配 profile。
3. 在开发 fixture 上传测试文件并产生测试记录，确认 production fixture 数据库字节和记录数不变。
4. 验证顺序启动/停止；条件允许时验证并行运行。
5. 验证两个环境的锁、日志、staging、audio、prompts、feedback 和本地会话数据库没有交叉。
6. 对现有正式路径只做只读路径和历史数据可见性验证，不执行真实 provider 调用。

## 13. 发布与回滚

实现阶段仅保留在 `codex/dev-prod-isolation`。不得改写、移动或重建 `v0.1.0-beta.1` 标签及资产。

用户确认候选版本验收后，才可以执行合并、推送和新版本发布。新版本建议为 `v0.1.0-beta.2`，必须重新生成 tar.gz 和 `.sha256`，不能复用 beta.1 资产。

升级验收必须证明：

- 安装前的正式数据库备份存在且可打开。
- 安装后默认数据库路径不变。
- 历史报告仍可见。
- 新的开发环境不会读取或改写正式历史。

若候选版本验证失败，回滚方式是停止开发实例并继续使用已发布的 beta.1 正式安装；不需要对正式数据库做逆向迁移，因为本设计不迁移正式数据。

## 14. 完成判据

只有同时具备以下证据，才能称为隔离完成：

- 自动化测试证明统一配置、危险路径拒绝和 Keychain service 分离。
- 两个健康端口返回各自正确 profile。
- 文件系统证据证明所有可写目录不交叉。
- 开发写入前后正式数据库无变化。
- production 完整回归、CLI、安装备份、前端测试和生产构建通过。
- 手动检查开发环境标识清晰，production 页面未出现误导标识。
- 没有访问真实 Keychain 明文、调用真实 provider 或修改现有 Release。
