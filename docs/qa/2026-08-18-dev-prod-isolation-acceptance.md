# 开发环境 / 正式环境隔离验收记录

执行日期：2026-08-19（Asia/Shanghai）

候选分支：`codex/dev-prod-isolation`

Task 7 起点：`6f996b6 fix: revalidate development runtime before writes`

发布基线：`0f7df7e` / `v0.1.0-beta.1`

## 结论

候选实现满足运行配置、可写数据、Keychain service、健康身份、开发写入、共享模型只读、安装历史保留和发布归档隔离要求。所有运行验收都使用临时 HOME、临时数据根、假 Security client、可观测且 fail-closed 的假 provider client，以及随机回环监听端口。

本次没有读取或改写用户真实正式数据库、Keychain、LaunchAgent、运行服务、音频、provider、远端 Git、标签或 Release，也没有执行 merge、push、tag 或发布。

## TDD 证据

### Task 7 原始红绿轮次

发布归档和安装器验收先得到 `2 failed, 6 passed in 2.80s`，分别证明原归档会带入伪造的运行数据/依赖/构建产物，且原安装器会接受缺少 `doctor_checks.py` 的包。最小修复后是 `8 passed in 2.12s`。

### 审查修正轮次

先将归档污染夹具改为大写/混合大小写目录，并将归档成员的每个路径组件 `casefold()` 后审查。旧 `find -name` 规则准确失败：

```text
1 failed in 0.50s
```

再给子进程夹具传入 provider 审计路径，要求每次应用启动必须生成可观测事件。旧夹具因 4 个审计文件全部为空而准确失败：

```text
1 failed, 1 deselected in 3.74s
```

修复后，大小写归档、完整边界快照和子进程夹具组合为：

```text
3 passed in 4.42s
```

提交前自审又加入混合大小写的 `.EnV.Secrets/secret.txt` 目录。当时只清理 `.env*` 普通文件的规则出现精确 RED（`1 failed in 0.42s`）；目录规则加入 `-iname '.env*'` 后单测为 `1 passed in 0.41s`。

发布/安装/备份/隔离的聚焦套件最终为：

```text
10 passed in 7.30s
```

## 完整临时边界证据

`complete_tree_snapshot()` 从测试临时根开始遍历，不跟随符号链接，对三类条目记录：

- 目录：类型、mode 与 `mtime_ns`；
- 普通文件：类型、mode、`mtime_ns`、size 与 SHA-256；
- 符号链接：类型、mode、`mtime_ns` 与原始 link target。

测试边界内额外放置一个文件和一个符号链接哨兵，证明快照确实包含不是普通文件的条目。开发 app 内实际执行设置修改、upload job 创建、本地 session 发放、反馈写入和开发音频哨兵写入。比较前后全部条目后，开发根之外的新增或改变集合精确为空。

子进程顺序验收中，惟一位于开发根之外的差异是事先创建并明确白名单化的测试观测文件：

```text
test-controls/sequential-development-provider-events.jsonl
```

该文件只记录假 provider client 的创建/拦截事件，不是应用产品数据。除去这一个明示的测试控制件后，开发根外无新增或改变条目，正式数据根的完整快照也逐条相同。

路径验收先解析 production/development 根及所有派生可写路径，显式断言两个根不相等、任一根都不是另一根的祖先，且两个解析后的可写集合不相交。开发根的 runtime lock、本地 session 数据库、主数据库、staging、prompts、feedback 和音频哨兵都被显式核对。

## 可观测 provider 边界

所有真实 app 夹具都在 lifespan 前替换 provider HTTP client。进程内夹具将 client 创建/调用事件收集到内存列表，production 和 development 各创建 5 个 fail-closed client，实测 provider 调用为 0。每个子进程则在 `create_app()` 前替换 `httpx.AsyncClient`，将工厂创建事件写入专用 JSONL；任何当前 provider 实际使用的 `post` 尝试（以及 `request/get/put/delete`）都会先写入 `provider_call` 事件，然后抛出 `ProviderNetworkBlocked`。因此边界既可观测，又 fail closed。

本轮 4 次应用启动各记录 5 个 `client_created/fail_closed=true` 事件。`provider_calls` 由 4 个 JSONL 中 `event == "provider_call"` 的实际条数求和，测量结果为 `0`；该数值不是手写常量。

## 双进程生命周期证据

命令：

```text
PYTHONPATH="$PWD/src" .venv/bin/pytest \
  tests/integration/test_runtime_isolation.py -q -s \
  -k sequential_and_simultaneous
```

结果：`1 passed, 1 deselected in 3.27s`。

| 阶段 | production | development |
|---|---:|---:|
| 顺序启动 PID | 30917 | 30918 |
| 并行启动 PID | 30919 | 30920 |
| 健康 profile | `production` | `development` |
| 逻辑端口 | 8765 | 8766 |
| 本轮随机回环端口 | 63778 | 63779 |

正式夹具数据库在只启动、写入并停止 development 前后：

```text
SHA-256 before: 2279e95541355776677ab998860c0a77d33c788ce45806933a7b9d6d9006e7cc
SHA-256 after:  2279e95541355776677ab998860c0a77d33c788ce45806933a7b9d6d9006e7cc
sentinel rows: 1 -> 1
```

共享模型哨兵在前后的 SHA-256 均为：

```text
5065f4cd3a31c202b7bc97c6826b6cf43592ee8c3e1a25ad12328ebcc5cecca5
```

本轮 pytest 临时根为：

```text
/private/var/folders/ys/89hz_yrn43xdbg0mh241_8tr0000gp/T/pytest-of-liujinxin/pytest-120/test_sequential_and_simultaneo0
```

子进程使用内嵌假 Security client；`read` 固定返回未配置，任何 `add`/`update` 立即失败。每个子进程只监听上表的随机 `127.0.0.1` 端口，没有检查或连接真实 8765/8766 服务。

## 发布归档与安装历史

发布测试不再使用或修改工作树中忽略的 `prototype/dist`。每次测试都在 `tmp_path/clean-checkout` 中复制发布必需的受控后端源码、迁移、元数据和脚本，并当场创建最小前端产物 `index.html`/`app.js`。归档输出也只写入该临时夹具。因此该测试可从干净 checkout 重现，不依赖忽略的本地构建产物，也不改写仓库构建输出。

污染夹具包含 `.RUNTIME`、`.VeNv`、`.GiT`、`.EnV.Secrets`、`Node_Modules`、`.UV-CACHE`、`.PyTeSt_CaChE`、`.MYPY_CACHE`、`.RuFf_CaChE`、`Fixture.EGG-INFO`、`TeStS`、`OuTpUtS`、`ScReEnShOtS`、`DeSiGnS`、`MoDeLs`、`AuDiO`、`BuIlD`、`__PyCaChE__`，以及大写扩展的数据库/音频/日志、`.env` 和符号链接。实现使用 `find -iname` 对目录与文件都做大小写不敏感清理；测试再对归档中的每个路径组件 `casefold()` 后扫描。

从临时隔离 checkout 生成的证据归档包含 191 个 tar 成员，其 SHA-256 和生成的 `.sha256` 文件一致：

```text
3bce3727f5aaf0f4cd155c82f583b7c85f3a627339704ab81f46f76fde36e339
```

归档保留 `main.py`、完整迁移链、最小前端产物，以及 `audio-memory`、`backup_data.py`、LaunchAgent 模板、`doctor.sh`、`doctor_checks.py`、`install-release.sh`、`install.sh`、`runtime_config.py`、`start.sh` 等安装/运行必需助手。

安装器测试使用临时 HOME 的默认正式路径，先安装 beta.1 夹具、写入一条历史记录，再安装 beta.2 夹具。顺序感知的假备份脚本观察到备份时 `current` 仍指向 beta.1，之后才切到 beta.2，原数据库行继续可读。默认正式数据库路径逐字符保持：

```text
<HOME>/Library/Application Support/AudioMemory/audio-memory.sqlite3
```

## 界面证据与非阻断项

Task 7 原始验收已在临时 Vite 和随机回环假后端上确认小尺寸、低饱和度的“开发环境”徽标，以及 production profile mismatch 时隐藏可写主界面并显示阻断卡片。假后端写入计数为 0，没有打开真实安装。截图位于 `.gitignore` 覆盖的 `prototype/output/playwright/`。本次审查修正没有改动前端，因此没有重复打开浏览器。

已知非阻断项保持不变：

1. 同一地址的后端被替换后，仅用于展示的缓存徽标不会自动刷新；每次 fetch 写入和每次 XHR send 都会重新检查 health，因此不会放行写入。
2. 已检入的“后端替换后再写入”回归覆盖 fetch；XHR 没有单独的替换场景测试，但共享的新鲜 guard 在每次 send（包含重试）执行。

## 完成判据映射

| 设计完成判据 | 直接证据 |
|---|---|
| 正式/开发健康身份 | 顺序与并行子进程的 health profile 分别为 `production` / `development` |
| 所有可写路径隔离 | 解析后根的祖先不重叠 + 全部派生可写集合 `isdisjoint` |
| 开发写入不改正式历史 | 设置/staging/session/feedback/audio 实际写入后，正式数据库 SHA/1 行哨兵和正式完整树不变 |
| 临时边界没有意外写入 | 文件/目录/符号链接快照；除开发根和明示的 provider 审计控制件外无差异 |
| 共享模型无写入 | `models_writable=False`，哨兵 SHA-256 不变 |
| provider 不能外联 | 4 个应用夹具各观察到 5 个 fail-closed client；审计实测 `provider_calls=0` |
| 归档无运行/秘密/依赖/构建产物 | 临时隔离 checkout 主动注入混合大小写污染，归档路径组件 casefold 扫描为空 |
| 归档包含安装/运行助手 | 所有必需脚本、`doctor_checks.py` 和 `runtime_config.py` 均在归档成员必需集合中 |
| 生产数据库路径与历史保留 | 精确默认路径断言 + 备份先于 `current` 切换 + 升级后原行可读 |

## 最终复验

提交前最后一轮完整命令结果：

```text
cd backend
PYTHONPATH="$PWD/src" .venv/bin/pytest -q
1014 passed, 28 skipped in 59.45s

cd ../prototype
node --test tests/*.test.mjs
91 passed, 0 failed, 0 skipped in 2.20s

npm run build
39 modules transformed; built in 396ms
Prepared Sites build: dist/server/index.js and dist/.openai/hosting.json

cd ..
bash -n scripts/*.sh
exit 0
```

28 项 skip 全部来自既有 legacy Event Map compatibility-only 标记，本 Task 没有新增 skip。

## 最终审查修正轮（2026-08-19）

本轮逐项核对了最终审查的 7 条意见；结论均为可复现缺陷或缺失验收，不需要技术反驳。修正边界如下：

| 审查项 | 技术处理与直接证据 |
|---|---|
| macOS 同目录别名 | 路径关系改为“已有祖先的 `st_dev/st_ino` 身份 + 未创建尾部的 Unicode/casefold 组件”比较；覆盖大小写变体及 `/Users` 与 `/System/Volumes/Data/Users` firmlink，拒绝后目标目录不存在。应用 lifespan 在首次目录写入前再次验证，符号链接在配置解析后被替换时仍 fail closed。 |
| Keychain 跨环境 service | `development` 拒绝 `Audio Memory`，`production` 拒绝 `Audio Memory Dev`；其他非空自定义 service 仍允许。环境解析和注入配置都在 Keychain client 构造/访问及目录创建前失败。 |
| 只读 Whisper 共享模型 | `models_writable=False` 时只从模型根旁的受控清单解析已安装本地 snapshot；清单缺失、身份不符、文件缺失或越界均失败，不把 repo ID 交给 `snapshot_download`。显式可写 development 模型根调用下载时固定 `cache_dir=<development model root>`。 |
| `create_app` 身份分裂 | `paths` 与 `local_port` 覆盖先合成为一个不可变的 effective `RuntimeConfig`，再统一验证、保存到 `app.state` 并供 health、Keychain、数据库和本地安全边界使用；development 注入正式路径或缺少正式边界均在零产品产物状态下拒绝。 |
| Doctor 模型位置 | `doctor-values` 现在输出经同一 resolver 派生的 manifest root；Whisper 与 diarization 都从共享模型根旁的 manifest 检查。隔离 development fixture 实际执行 Whisper 校验、带隔离信任集合的 diarization 语义校验、迁移/import/Prompt/可写性检查，Doctor 退出码为 0。 |
| `MODELS_WRITABLE` 输出 | 选择删除误导性的 `AUDIO_MEMORY_MODELS_WRITABLE` shell 输出；可写状态仍是 `RuntimeConfig.paths.models_writable` 的内部类型化状态，下载边界直接消费该状态，不再尝试从未解析的环境变量 round-trip。 |
| LaunchAgent 身份 | 非 core 的 production Doctor 精确检查 `launchctl print gui/<uid>/com.audio-memory.local`；development 同一测试证明既不调用 `launchctl`，也不调用 Keychain 可访问性检查。 |

TDD RED 记录：路径别名 `2 failed`；跨环境 Keychain `2 failed`；effective runtime 三项 `3 failed`；写前复验 `1 failed`；缺少正式边界 `1 failed`；Whisper 初始三项 `3 failed`，只读自定义 repo fallback `1 failed`；Doctor 路径与 LaunchAgent 组合 `2 failed`。每组失败原因均为待实现行为缺失，随后对应聚焦测试转绿。

最终证据（下列结果取代本文件前一节的旧计数）：

```text
聚焦审查回归：74 passed, 29 deselected in 16.88s
临时双环境/边界验收：2 passed in 4.07s

后端全量：1032 passed, 28 skipped in 47.13s
前端全量：91 passed, 0 failed, 0 skipped in 2.46s
前端生产构建：39 modules transformed; built in 446ms
Shell 语法：exit 0
```

首次在受限沙箱中运行回环监听测试时，后端与前端分别出现 `PermissionError/EPERM`；获准仅使用随机 `127.0.0.1` 临时端口后，后端隔离验收和前端代理测试全部通过。该差异来自沙箱禁止监听，不是产品失败。

本轮仍只使用临时目录、假 Security client、fail-closed provider 和隔离模型文件；没有读取或改写真实正式数据库、真实 Keychain、真实模型缓存、LaunchAgent 或运行服务，没有 provider 外联，也没有访问远端 Git、标签或 Release。
