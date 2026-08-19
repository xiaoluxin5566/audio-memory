# 开发环境 / 正式环境隔离验收记录

执行日期：2026-08-19（Asia/Shanghai）

候选分支：`codex/dev-prod-isolation`

Task 7 起点：`6f996b6 fix: revalidate development runtime before writes`
发布基线：`0f7df7e` / `v0.1.0-beta.1`

## 结论

候选实现满足设计中的运行配置、可写数据、Keychain service、健康身份、开发写入、共享模型只读、安装历史保留和发布归档隔离要求。所有运行验收都使用临时 HOME、临时数据根、假 Security client 和随机回环监听端口；未读取或改写用户真实正式数据库、Keychain、LaunchAgent、运行服务、音频、provider、远端 Git、标签或 Release。

`v0.1.0-beta.1` 仍然只是既有发布基线。本次没有合并、推送、打标签或发布 `v0.1.0-beta.2`，等待用户明确决定。

## TDD 与自动化证据

### 运行隔离集成

新增 `backend/tests/integration/test_runtime_isolation.py` 后首次运行：

```text
PYTHONPATH="$PWD/src" .venv/bin/pytest tests/integration/test_runtime_isolation.py -q
1 passed in 0.79s
```

这项验收在 Tasks 1–6 的实现上直接通过，因此没有制造虚假的运行时 RED，也没有扩大后端改动。测试创建 production/development 两个 `RuntimeConfig` 和应用实例，枚举所有可写路径，分别核对健康 profile，执行开发数据库、设置、staging、本地会话、反馈和音频目录写入，并比较正式夹具整棵文件树、数据库 SHA-256/行数及共享模型哨兵。

### 发布与安装 RED

先扩展发布归档和安装器测试，再运行：

```text
PYTHONPATH="$PWD/src" .venv/bin/pytest \
  tests/unit/test_release_package.py \
  tests/unit/test_release_installer.py \
  tests/unit/test_backup_data.py -q
2 failed, 6 passed in 2.80s
```

两个预期失败分别证明：

1. 原归档会带入前端构建目录中伪造的 `.runtime`、`.env.production`、SQLite、音频、日志、models、tests、outputs、build、`__pycache__` 和本地依赖符号链接。
2. 原安装器会接受缺少 `scripts/doctor_checks.py` 的不完整发布包，使已安装 Doctor 的运行依赖不完整。

最小修复后：

```text
8 passed in 2.12s
```

修复仅清理发布暂存区中的禁带目录、禁带文件类型和符号链接，并把 `doctor_checks.py` 加入安装前强制清单。安装行为测试还证明默认数据库路径逐字符保持不变，并通过一个顺序感知的临时备份脚本证明备份发生在 `current` 切换之前；升级后历史行仍可读取。

提交前自审继续扩展污染夹具，覆盖 SQLite `-wal/-shm/-journal`、常见音频扩展、滚动日志、`.uv-cache`/pytest/mypy/ruff 缓存、egg-info 和大写扩展；旧规则再次出现预期 RED：

```text
1 failed in 0.46s
```

补齐大小写不敏感的禁带规则后，单项归档测试 `1 passed in 0.44s`，发布/安装/备份组合最终为 `8 passed in 3.58s`。

### 完整回归

最终提交前完整回归结果记录在本文件的“最终复验”小节。前一轮完整基线结果为：

```text
backend: 1013 passed, 28 skipped in 38.33s
prototype: 91 passed, 0 failed, 0 skipped
Vite production build: 39 modules transformed, success
bash -n scripts/*.sh: success
```

28 项跳过均为已标注的 legacy Event Map compatibility-only 测试，不是本次新增跳过。受限沙箱首次拒绝 4 个 Node 回环监听测试（`listen EPERM`）；授予仅随机 loopback 监听后，同一完整前端命令 91/91 通过。没有功能失败或意外警告。

## 双进程生命周期验收

命令：

```text
PYTHONPATH="$PWD/src" .venv/bin/pytest \
  tests/integration/test_runtime_isolation.py -q -s \
  -k sequential_and_simultaneous
```

最终结果：`1 passed, 1 deselected in 3.94s`。

为遵守“回环测试只能使用随机临时端口”，配置的逻辑 production/development 端口仍分别是 `8765`/`8766`，实际监听映射到本次临时端口 `53933`/`53934`。应用自身收到的 profile 和数据根仍来自真实 `RuntimeConfig`；只有监听端口通过 `create_app(local_port=...)` 的既有测试注入边界替换。

| 阶段 | production | development |
|---|---|---|
| 顺序启动 PID | `56494` | `56495` |
| 顺序健康 profile | `production` | `development` |
| 并行启动 PID | `56496` | `56497` |
| 并行健康 profile | `production` | `development` |
| 逻辑端口 | `8765` | `8766` |
| 临时监听端口 | `53933` | `53934` |

本次 pytest 临时根：

```text
/private/var/folders/ys/89hz_yrn43xdbg0mh241_8tr0000gp/T/pytest-of-liujinxin/pytest-79/test_sequential_and_simultaneo0
```

子进程使用内嵌假 Security client；`read` 固定返回未配置，任何 `add`/`update` 都立即失败。记录为 `fake_keychain=true`、`provider_calls=0`。验收夹具没有真实 API Key，因此不会进入 provider 验证或分析调用。

正式数据库在只启动/写入/停止 development 前后保持：

```text
before SHA-256: 189c8091306ef07168b576e51ec1a2ea793a4b8f531f6d9e7e557a96fa3cd340
after  SHA-256: 189c8091306ef07168b576e51ec1a2ea793a4b8f531f6d9e7e557a96fa3cd340
before rows: 1
after  rows: 1
```

共享模型哨兵同样没有写入：

```text
before SHA-256: 5065f4cd3a31c202b7bc97c6826b6cf43592ee8c3e1a25ad12328ebcc5cecca5
after  SHA-256: 5065f4cd3a31c202b7bc97c6826b6cf43592ee8c3e1a25ad12328ebcc5cecca5
```

夹具进程先逐个启动/停止，再同时保持在线并重新查询两个健康端点。所有子进程都收到 TERM 并正常退出，没有使用进程名宽泛查杀，也没有检查或连接真实 8765/8766 服务。

## 可写路径矩阵

矩阵中的 `<temp-home>` 和 `<temp-project>` 都位于上述 pytest 临时根。集合测试使用绝对解析路径做 `isdisjoint`，并检查 development 产生的每个文件都位于 development 根内。

| 资源 | production fixture | development fixture | 交叉 |
|---|---|---|---|
| 数据根 | `<temp-home>/Library/Application Support/AudioMemory` | `<temp-project>/.runtime/dev` | 无 |
| 数据库 | `.../AudioMemory/audio-memory.sqlite3` | `.../.runtime/dev/audio-memory.sqlite3` | 无 |
| runtime | `.../AudioMemory/runtime` | `.../.runtime/dev/runtime` | 无 |
| 实例锁 | `.../runtime/audio-memory.lock` | `.../runtime/audio-memory.lock` | 无 |
| 本地会话库 | `.../runtime/local-web-security.sqlite3` | `.../runtime/local-web-security.sqlite3` | 无 |
| 日志 | `.../runtime/audio-memory.log` | `.../runtime/audio-memory-dev.log` | 无 |
| 开发 PID/启动锁 | 不适用 | `.../runtime/audio-memory-dev.pid` / `.start.lock` | 无 |
| staging | `.../AudioMemory/staging` | `.../.runtime/dev/staging` | 无 |
| audio | `.../AudioMemory/audio` | `.../.runtime/dev/audio` | 无 |
| prompts | `.../AudioMemory/prompts` | `.../.runtime/dev/prompts` | 无 |
| feedback | `.../AudioMemory/意见反馈` | `.../.runtime/dev/意见反馈` | 无 |
| models | `.../AudioMemory/models`，可写 | 同一路径，只读共享 | 不属于 development 可写集合；哈希不变 |

实际文件清单分别只出现于 `home/Library/Application Support/AudioMemory/...` 和 `project/.runtime/dev/...`。production 的默认数据库路径仍精确为：

```text
<HOME>/Library/Application Support/AudioMemory/audio-memory.sqlite3
```

## Release 白名单与安装历史

独立临时归档（完成清单与哈希记录后已删除临时目录）：

```text
/private/tmp/audio-memory-task7-release.pJ3Uzn/audio-memory-v0.1.0-beta.1-macos-arm64.tar.gz
entries: 192
SHA-256: 6b9b4e4243702faa7eac47e8393621d19593823ec69af6e6b15ab027e447a765
```

确认包含：

- `VERSION`、README/CHANGELOG/PRIVACY；
- `backend/pyproject.toml`、`uv.lock`、`alembic.ini`、`src/audio_memory/main.py` 和完整迁移链（含 `0014_app_settings.py`）；
- `prototype/dist/client/index.html` 与前端生产静态资源；
- `audio-memory`、`backup_data.py`、LaunchAgent 模板、`doctor.sh`、`doctor_checks.py`、`install-release.sh`、`install.sh`、`runtime_config.py`、`start.sh`。

确认排除：

- `.git`、`.venv`、`.runtime`、`node_modules` 和任何符号链接；
- `.env*`、SQLite/DB 的 WAL/SHM/journal 伴随文件、日志/滚动日志和缓存字节码；
- MP3/AAC/M4A/WAV/FLAC/OGG/OPUS/WMA/CAF/AIFF（扩展大小写不敏感）、models、audio；
- `.uv-cache`、pytest/mypy/ruff 缓存、egg-info、tests、outputs、screenshots、designs 和嵌套 build 目录。

测试会在被复制的前端目录中主动放置每一种伪造污染物再构建归档，因此不是只对当前干净目录做名称扫描。安装器测试使用临时 HOME 的默认正式路径，先安装 beta.1 fixture、写入一条“历史报告”，再安装 beta.2 fixture；备份脚本观察到 `current` 仍指向 beta.1，之后才切到 beta.2，原数据库行继续可读。

## 界面人工检查

只打开临时 Vite `127.0.0.1:54151`，其后端是随机回环 `127.0.0.1:54150` 上的测试 app；安全数据库位于 `/private/tmp/audio-memory-task7-ui.PRCNCG/`。没有打开真实安装；截图落盘后临时服务和该 `/private/tmp` 目录均已关闭/删除。

- development profile：顶部现有品牌区域旁显示小尺寸、低饱和描边的“开发环境”徽标。它持续可见、容易辨认，但不改变上传、Feed、历史或设置布局。
- production profile mismatch：把同一个测试后端切换为 `production` 后重新加载，页面隐藏可写主界面，只显示“已停止本地写入 / 开发界面未连接到开发服务”的阻断卡片。测试后端写入计数保持 `0`。

截图（均位于 `.gitignore` 已覆盖的 `prototype/output/`）：

- `prototype/output/playwright/development/.playwright-cli/page-2026-08-19T05-33-52-022Z.png`
- `prototype/output/playwright/profile-mismatch/.playwright-cli/page-2026-08-19T05-34-55-656Z.png`

轻量测试后端只实现 health/session/effect，未实现完整 Feed/设置读取 API，因此开发页面控制台存在预期的 fixture 404；这不涉及真实服务，且不影响徽标/阻断态判断。完整前端自动化命令没有失败或警告。

## 已知非阻断项

1. 后端在同一地址从 development 被替换为 production 后，顶部仅展示用的缓存徽标不会自动刷新；任何 fetch 写入会重新检查 health，XHR 每次 send（含重试）也走新鲜 guard。人工验收通过重新加载确认阻断态。该问题只影响替换后的显示即时性，不会放行写入。
2. 已检入的“后端替换后再次写入”测试覆盖 fetch；没有单独的 XHR 替换场景测试。现有 XHR 重试测试和共享的新鲜 guard 覆盖每次 send，代码审查未发现绕过路径。保留为后续增强，不作为本次隔离发布阻断项。

## 安全边界确认

- 未读取、哈希、迁移、复制或改写用户真实 `~/Library/Application Support/AudioMemory`。
- 未读取或写入真实 Keychain；所有应用启动均注入假 Security client。
- 未调用 provider，未使用真实音频；应用验收流量只访问随机 loopback 夹具。
- 未加载、创建或停止真实 LaunchAgent，未检查或连接真实运行服务。
- 未执行 merge、push、tag、GitHub Release 或任何远端 Git 操作。
- 当前隔离工作树没有本地 `v0.1.0-beta.1` tag ref；本 Task 未执行任何 tag 或远端命令，因此没有创建、删除或改写该标签，也没有修改 Release。临时归档只写入 `/private/tmp`。

## 完成判据映射

| 设计完成判据 | 证据 |
|---|---|
| 统一配置、危险路径、Keychain service 分离 | Tasks 1–5 回归 + 本次两个 profile 的真实应用启动和假 Keychain service 断言 |
| 两个健康端口/profile 正确 | 逻辑 8765/8766；安全随机监听 53933/53934；顺序和并行健康响应均匹配 |
| 所有可写目录不交叉 | `writable_paths(...).isdisjoint(...)` + 路径矩阵 + 实际文件清单 |
| 开发写入不改变正式历史 | 数据库 SHA-256 相同、sentinel 1 行保持 1 行、production 全树快照相同 |
| 共享模型只读 | `models_writable=False`；模型哨兵 SHA-256 相同 |
| production 回归、CLI、安装备份、前端和构建 | 完整后端/前端套件，发布/安装/备份 8/8，Vite build，shell syntax |
| 开发标识和误连阻断 | 两张临时 fixture 截图；production mismatch 写入计数 0 |
| 真实系统与现有 Release 未触碰 | 上述安全边界确认 + 最终 Git/tag/diff 检查 |

## 最终复验

提交前最后一次完整命令结果：

```text
cd backend
PYTHONPATH="$PWD/src" .venv/bin/pytest -q
1014 passed, 28 skipped in 46.94s

cd ../prototype
node --test tests/*.test.mjs
91 passed, 0 failed, 0 skipped in 2.24s

npm run build
39 modules transformed; built in 403ms
Prepared Sites build: dist/server/index.js and dist/.openai/hosting.json

cd ..
bash -n scripts/*.sh
exit 0
```

最终后端轮次包含本 Task 新增的两个隔离集成测试；没有 warning summary。28 个 skip 全部来自既有 legacy Event Map compatibility-only 标记。前端、构建和 shell 语法均为该轮代码上的新鲜结果。
