# beta.3 旧分支价值审计

审计日期：2026-08-19  
集成基线：`main@d65c3c0`  
审计分支：`codex/beta3-stability`

## 结论摘要

| 分支 | 状态 | 结论 | 删除前置条件 |
| --- | --- | --- | --- |
| `codex/analysis-sleep-prevention` | 工作树干净 | `equivalent-on-main` | 确认 `main@1ce96dc` 回归通过后可删除 |
| `codex/smooth-progress` | 仅未跟踪依赖目录 | `equivalent-on-main` | 确认 `main@b3356a6` 回归通过；依赖目录不迁移 |
| `codex/dev-prod-isolation` | 4 个业务/测试文件未提交 | `migrate-to-beta3` | 迁移虚拟环境启动修复；按新设计重写交接修复并通过回归 |
| `codex/cloud-asr-evaluation` | 工作树干净 | `retain-as-research-evidence` | 抽取最终方法、结论和复现入口到当前基线；不迁移生产代码 |
| `codex/report-audit-revision-pipeline` | 主工作树高度脏 | 混合：已合并、研究成果和临时产物 | 完成下述文件组处置并验证后才能移除工作树/分支 |

当前 `git branch --no-merged main` 除本审计分支外，正好是以上五个旧分支；没有遗漏其他未合并本地分支。

## 1. `codex/analysis-sleep-prevention`

- 分叉点：`85e01ff`
- 分支顶端：`92a07f3 feat: prevent sleep during audio analysis`
- 状态：工作树干净。
- 等价证据：`main` 已包含 `1ce96dc feat: prevent sleep during audio analysis`；核心迁移、设置 API、`SleepPreventionManager`、协调器资源回收和测试均已存在于当前基线。
- 处置：`equivalent-on-main`。不再次迁移，避免重复数据库迁移和冲突。
- 删除门槛：运行防休眠、设置 API、上传任务和应用关闭回归；确认当前实现覆盖分支测试。

## 2. `codex/smooth-progress`

- 分叉点：`85e01ff`
- 分支顶端：`92feabf feat: smooth transcription progress between checkpoints`
- 状态：只有 `backend/.venv` 与 `prototype/node_modules` 未跟踪目录，均为可重建依赖，不是产品成果。
- 等价证据：`main` 已包含 `b3356a6 feat: smooth transcription progress between checkpoints`；当前 `main` 的进度实现随后继续演进，不能用旧文件覆盖。
- 处置：`equivalent-on-main`。依赖目录不迁移。
- 删除门槛：运行 ETA、上传进度、前端状态和相关端到端测试。

## 3. `codex/dev-prod-isolation`

- 分叉点：`339a067`
- 已提交独有提交：`3deeaa0 fix: launch development with virtualenv python`。
- 主体等价证据：`main` 已包含从 `7c389a7` 到 `ac61db1` 的开发/正式隔离系列提交，包括固定开发根、别名防护、生命周期身份和合并提交。

### 待迁移文件

| 文件 | 价值结论 | beta.3 处置 |
| --- | --- | --- |
| `scripts/dev_lifecycle.py` | 虚拟环境解释器选择有价值 | 基于当前 `main` 重新实现并测试 |
| `backend/tests/unit/test_dev_scripts.py` | 防止回退到系统 Python 的测试有价值 | 迁移为当前基线回归测试 |
| `backend/src/audio_memory/analysis/task_coordinator.py` | commit 后取消仍通知 worker 的思路有价值 | 不直接复制；纳入原子交接与真实 worker 竞争测试 |
| `backend/src/audio_memory/api/jobs.py` | 超时、持久状态复核、失败回收有价值 | 不直接复制；当前补丁会在取消时留下假 `analyzing`，按新状态机重写 |
| `backend/tests/integration/test_transcription_recovery.py` | 转写保留、超时、取消、commit 后异常测试有价值 | 迁移并补齐取消状态和资源所有权断言 |
| `backend/tests/unit/analysis/test_task_coordinator.py` | 真实 worker 唤醒竞态测试有价值 | 迁移到新的 cancellation-safe 通知实现 |

删除门槛：以上价值全部进入 `codex/beta3-stability` 的独立提交，相关测试和全量回归通过，并再次确认旧工作树没有新增内容。

## 4. `codex/cloud-asr-evaluation`

- 分叉点：`24e0867`
- 独有提交：34 个。
- 规模：41 个文件，约 20,112 行新增；工作树干净。
- 内容：阿里云与火山 ASR 适配、分块合并、证据清单、事实对比、盲审包、报告生成、CLI 和完整测试。
- 产品判断：用户当前版本的核心承诺是本地转写；把云 ASR 运行时代码合入 beta.3 会扩大凭据、网络、隐私和维护面，不符合本次稳定性目标。
- 价值判断：评测方法、事实审计原则、清单 schema、已验证的错误处理结论具有研究价值。
- 处置：`retain-as-research-evidence`。后续只迁移一份精简方法/结论文档及必要 schema 说明；不迁移 `audio_memory/transcription/cloud/` 到生产包，不迁移供应商凭据或真实运行产物。
- 删除门槛：精简证据文档可在当前基线独立阅读并给出原分支/提交引用；确认未来如需复现实验可从标签或归档提交恢复。

## 5. `codex/report-audit-revision-pipeline` 与主工作树

- 分叉点：`85e01ff`
- 已提交顶端：`a61234b`。
- 提交等价性：设计、计划和 provider 范围修复已以等价补丁进入 `main`；`dfe9f8f` 的大提交补丁 ID 不等价，但当前 `main` 已包含后来演进的直接报告、分段审计、恢复和 GLM 审计链，禁止整分支合并。
- 状态：主工作树包含大量已修改与未跟踪文件，是仍在演进的研发集合；当前不可删除、不可强制清理。

### 已跟踪修改文件组

以下路径下的所有已跟踪修改逐文件归入对应处置；完整文件名以本次 `git status --short` 快照为准：

| 文件/路径组 | 处置 |
| --- | --- |
| `backend/src/audio_memory/analysis/{provider,publisher,runner,task_coordinator}.py`、`api/*.py`、`main.py` | 与当前生产链交叉；逐功能对照 `main`，只迁移 beta.3 已批准的交接/恢复/日志价值 |
| `backend/src/audio_memory/providers/**`、`repositories.py` | GLM/provider 演进；当前 `main` 已有正式实现，默认 `equivalent-on-main`，差异须有独立测试才迁移 |
| `backend/src/audio_memory/transcription/{checkpoints,engine}.py` | 转写实验；不得覆盖 beta.2 基线，只有诊断/去重价值经独立测试后迁移 |
| `backend/src/audio_memory/prompts/**` | Prompt/报告质量研究；不属于本次稳定性修复，保留为研究证据，另行质量验收后才能进入生产 |
| `backend/tests/**` | 测试本身有证据价值；随其验证的功能迁移，不能单独证明生产代码可合并 |
| `prototype/src/**`、`prototype/tests/**`、`prototype/vite.config.mjs` | UI 与安全实验；只迁移持久分析状态展示及已验证的安全修复 |
| `scripts/{doctor,doctor_checks,start}.sh`、`tests/real-pipeline-smoke.py` | doctor/启动价值与 beta.3 目标相关；基于当前 `main` 重写并用假 provider 验证 |
| `backend/migrations/env.py` | SQLite 配置价值相关；不复制旧差异，按 WAL/busy timeout 新设计实现 |

### 未跟踪成果文件组

| 文件/路径组 | 处置 |
| --- | --- |
| `backend/src/audio_memory/analysis/*.py` 新实验模块及对应 `backend/tests/**` | 研究价值高但不在 beta.3 稳定性范围；先保留，后续单独产品评审，当前删除阻塞 |
| `backend/src/audio_memory/transcription/{deduplication,diagnostics}.py` 及测试 | 诊断可能服务本次可观测性；只迁移无正文泄漏的通用诊断，其余保留研究 |
| `backend/src/audio_memory/providers/adapters/glm.py`、provider 模型选择测试 | 当前 `main` 已有 GLM 正式实现；对照后按 `equivalent-on-main` 处置 |
| `backend/src/audio_memory/prompts/*.md` 新 Prompt 与 Prompt 测试 | 报告质量研究，暂不进入 beta.3 稳定性提交；保留研究证据 |
| `docs/HANDOFF-*`、`docs/PROJECT-*`、`docs/superpowers/**`、`docs/benchmark-evidence/**`、`design-qa.md` | 文档证据应筛选迁入当前基线；涉及已完成主线的归档，重复草稿不迁移 |
| `backend/experiments/`、`backend/tests/experiments/`、`tests/real-*.py` | 离线实验/真实评测；不进入发布包，保留最小复现说明后归档 |
| `prototype/src/mockEngine.js`、`prototype/tests/mock-engine.test.mjs`、`prototype/output/**` | fake provider 对 beta.3 验收有价值；迁移通用假实现与测试，不迁移截图/生成输出 |
| `.playwright-cli/`、`.private-eval/`、`.superpowers/brainstorm/`、`.vite/`、`audio-memory.sqlite3`、`outputs/` | 本地状态、私有评测、缓存、数据库或生成物；绝不提交，不作为代码迁移 |
| `docs/assets/`、其他输出图片/JSON | 逐项确认是否被正式文档引用；未引用生成物不迁移 |

### 当前处置

该分支不是“无用分支”，而是尚未拆分的混合研发工作树。先迁移 beta.3 范围内的诊断、fake provider 和安全测试；报告体系实验另立产品评审。未完成拆分前保持原状，不删除。

## 删除与集成规则

1. 任何旧工作树在删除前重新执行 `git status --short`，防止审计后新增成果。
2. 有价值代码必须从当前 `main` 重新实现或选择性迁移，并有独立测试；禁止整分支 merge。
3. 未跟踪依赖、缓存、数据库和输出不进入 Git；删除它们仍需和工作树删除一起经过用户批准。
4. 先验证迁移提交，再移除工作树，再删除本地分支；远程分支单独列出并批准。
5. Release 标签永久保留。真实 Keychain、正式数据和真实 provider 不属于本次审计读取范围。
