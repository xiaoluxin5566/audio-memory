# 功能开发轨道与发版治理实施计划

> **供编码代理执行：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐项执行。每个步骤使用复选框跟踪。

**目标：** 用仓库内的功能轨道、环境隔离、测试门禁和双重发布授权，强制执行 beta.2 → beta.3 开发发布流程。

**架构：** 新增一个无用户数据的 Python 流程核心，负责验证 Git/worktree/开发进度记录和原子更新；进度记录位于 `git rev-parse --git-common-dir` 下的 `audio-memory-governance`，不参与 Git 提交。薄 shell 入口只负责定位仓库与调用核心。开发运行复用已有 `dev_lifecycle.py`，发布复用 `build-release.sh`。

**技术栈：** Python 3.12、Bash、Git worktree、pytest、Node.js test runner、Playwright、现有 Vite/FastAPI 开发运行时。

**设计文档：** `docs/superpowers/specs/2026-08-19-feature-track-and-release-governance-design.md`

## 全局约束

- 用户使用版本继续固定为 `v0.1.0-beta.2`，本计划不合并 `main`、不发布、不安装新版。
- 新功能分支名必须是 `codex/<feature_id>`，且必须从干净 `main` 创建。
- 功能可跨多个对话，但同一 `feature_id` 只有一个分支、一个 worktree 和一份共享开发进度记录。
- 开发页面是 `127.0.0.1:5173`，开发后端是 `127.0.0.1:8766`，数据根是功能 worktree 内的 `.runtime/dev`。
- 没有用户明确批准，任何命令都不合并、不发布、不创建标签、不删除分支或 worktree。
- 所有生产代码遵守 TDD：先观察目标测试失败，再实现最小代码。

---

### 任务 1：功能状态模型与安全存储

**文件：**
- 新建：`scripts/feature_governance.py`
- 新建：`backend/tests/unit/test_feature_governance.py`

**接口：**
- 输出：`FeatureRecord`、`FeatureStore`、`GitRepository`、`GovernanceError`
- 供后续任务使用：`FeatureStore.load(feature_id)`、`FeatureStore.save(record)`、`GitRepository.snapshot()`

- [ ] **步骤 1：先写非法标识符与状态文件路径越界的失败测试**

```python
@pytest.mark.parametrize("value", ["", "../main", "A B", "feature/child"])
def test_feature_id_rejects_path_and_branch_injection(value):
    with pytest.raises(GovernanceError):
        FeatureRecord.new(value, base_commit="a" * 40)

def test_store_rejects_symlinked_features_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "features").symlink_to(outside)
    with pytest.raises(GovernanceError):
        FeatureStore(tmp_path).save(FeatureRecord.new("search", "a" * 40))
```

- [ ] **步骤 2：运行测试并确认因接口尚未存在而失败**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py -q
```

预期：测试收集失败，明确指向 `feature_governance` 未实现。

- [ ] **步骤 3：实现严格模型、JSON 校验和原子写入**

`FeatureRecord` 必须固定 `schema_version=1`，验证状态枚举、分支名、相对 worktree 路径、Git SHA 和检查名。`FeatureStore.save()` 必须在 Git 共享管理目录的 `audio-memory-governance/features` 中使用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`；拒绝符号链接、硬链接和非普通文件。

- [ ] **步骤 4：增加往返、未知字段、损坏 JSON 和原子替换测试并运行**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py -q
```

预期：全部通过。

- [ ] **步骤 5：提交状态存储单元**

```bash
git add scripts/feature_governance.py backend/tests/unit/test_feature_governance.py
git commit -m "feat: add durable feature track state"
```

### 任务 2：新建与恢复功能轨道

**文件：**
- 修改：`scripts/feature_governance.py`
- 新建：`scripts/feature-start.sh`
- 新建：`scripts/feature-status.sh`
- 修改：`backend/tests/unit/test_feature_governance.py`

**接口：**
- 新增：`FeatureService.start(feature_id, target_version)`
- 新增：`FeatureService.status(feature_id=None)`
- 命令：`feature-start.sh <feature_id> [--target-version v0.1.0-beta.3]`、`feature-status.sh [feature_id]`

- [ ] **步骤 1：先写临时 Git 仓库中的干净 main 创建测试**

```python
def test_start_creates_one_branch_worktree_and_record(git_fixture):
    result = git_fixture.service.start("report-progress", "v0.1.0-beta.3")
    assert result.record.branch == "codex/report-progress"
    assert result.record.worktree == ".worktrees/report-progress"
    assert git_fixture.branch_exists("codex/report-progress")
    assert git_fixture.worktree_branch(result.path) == "codex/report-progress"
```

- [ ] **步骤 2：先写“已存在时恢复”和“状态不一致时停止”测试**

```python
def test_start_existing_feature_resumes_without_new_branch(git_fixture):
    first = git_fixture.service.start("search", "v0.1.0-beta.3")
    second = git_fixture.service.start("search", "v0.1.0-beta.3")
    assert second.path == first.path
    assert git_fixture.branches_named("codex/search") == 1

def test_start_refuses_record_whose_worktree_uses_another_branch(git_fixture):
    git_fixture.replace_worktree_branch("search", "codex/other")
    with pytest.raises(GovernanceError, match="worktree.*branch"):
        git_fixture.service.start("search", "v0.1.0-beta.3")
```

- [ ] **步骤 3：运行测试并确认缺少轨道服务而失败**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py -q
```

- [ ] **步骤 4：实现 Git 校验、worktree 创建和只读状态列表**

所有 Git 调用使用参数数组和 `check=False`，不经 shell 解析。只允许从干净 `main` 创建新轨道。如果分支、worktree 或状态文件只存在其中一部分，输出诊断并停止，不自动删除或覆盖。

- [ ] **步骤 5：运行单元测试和 shell 入口帮助测试**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py -q
```

- [ ] **步骤 6：提交功能轨道创建与恢复**

```bash
git add scripts/feature_governance.py scripts/feature-start.sh scripts/feature-status.sh backend/tests/unit/test_feature_governance.py
git commit -m "feat: create and resume feature tracks"
```

### 任务 3：5173 前端与 8766 后端的联合开发生命周期

**文件：**
- 修改：`scripts/dev_lifecycle.py`
- 修改：`scripts/dev-start.sh`
- 修改：`scripts/dev-stop.sh`
- 修改：`scripts/feature_governance.py`
- 修改：`backend/tests/unit/test_dev_scripts.py`
- 修改：`backend/tests/unit/test_feature_governance.py`

**接口：**
- `dev-start.sh` 成功后同时维护开发后端和 Vite 前端记录
- `dev-stop.sh` 只停止当前 worktree 拥有的两个进程
- `feature-start` 成功输出 `http://127.0.0.1:5173`

- [ ] **步骤 1：先写开发启动器同时验证 8766/development 和 5173 的失败测试**

```python
def test_development_start_reports_browser_url_only_after_both_services_ready(...):
    result = run_start(..., backend_health='{"profile":"development"}', frontend_status=200)
    assert result.returncode == 0
    assert "http://127.0.0.1:5173" in result.stdout

def test_development_start_rejects_production_backend_identity(...):
    result = run_start(..., backend_health='{"profile":"production"}')
    assert result.returncode != 0
    assert "正式环境" in result.stderr
```

- [ ] **步骤 2：运行目标测试并确认当前只启动后端而失败**

```bash
cd backend
.venv/bin/pytest tests/unit/test_dev_scripts.py tests/unit/test_feature_governance.py -q
```

- [ ] **步骤 3：扩展受守护的进程记录与精确停止**

为 Vite 保存独立 PID、启动时间和完整 argv；停止前同样验证 PID 身份，禁止按进程名全局停止。后端身份必须为 `development`，前端必须使用当前 worktree 的 `prototype`。

- [ ] **步骤 4：增加端口占用、进程替换、只启动一半和两个 worktree 冲突测试**

端口 5173/8766 已被其他 worktree 占用时，输出拥有者并停止，不取代对方进程。

- [ ] **步骤 5：运行开发脚本与前端安全回归**

```bash
cd backend
.venv/bin/pytest tests/unit/test_dev_scripts.py tests/unit/test_feature_governance.py -q
cd ../prototype
node --test tests/dev-proxy-security.test.mjs tests/runtime-environment.test.mjs
```

- [ ] **步骤 6：提交联合开发生命周期**

```bash
git add scripts/dev_lifecycle.py scripts/dev-start.sh scripts/dev-stop.sh scripts/feature_governance.py backend/tests/unit/test_dev_scripts.py backend/tests/unit/test_feature_governance.py
git commit -m "feat: manage the complete development runtime"
```

### 任务 4：功能完成门禁与提交失效机制

**文件：**
- 修改：`scripts/feature_governance.py`
- 新建：`scripts/feature-finish.sh`
- 新建：`scripts/quality-gate.sh`
- 修改：`backend/tests/unit/test_feature_governance.py`

**接口：**
- `FeatureService.finish(feature_id, gate_runner)`
- `quality-gate.sh [--scope feature|integration|release]`

- [ ] **步骤 1：先写工作区不干净、分支不匹配和门禁失败的测试**

```python
def test_finish_never_marks_dirty_or_failed_feature_ready(git_fixture):
    git_fixture.start("search")
    git_fixture.make_uncommitted_change("search")
    with pytest.raises(GovernanceError, match="未提交"):
        git_fixture.service.finish("search", passing_gate)
    assert git_fixture.record("search").status == "in_progress"
```

- [ ] **步骤 2：先写通过后记录确切 SHA，以及新提交使证据失效的测试**

```python
def test_new_commit_invalidates_ready_evidence(git_fixture):
    git_fixture.finish("search")
    assert git_fixture.record("search").status == "ready_to_merge"
    git_fixture.commit("search", "later change")
    status = git_fixture.service.status("search")
    assert status.effective_status == "in_progress"
    assert status.passed_checks == ()
```

- [ ] **步骤 3：运行测试并确认 finish 接口缺失**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py -q
```

- [ ] **步骤 4：实现三级门禁执行器和状态更新**

feature 范围运行后端完整测试、前端 Node 测试、Playwright、构建和运行隔离检查。每个检查只记录名称、结果、时间和提交，不将完整日志写入 Git。

- [ ] **步骤 5：用替身门禁测试状态机，再运行真实相关门禁**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py tests/integration/test_runtime_isolation.py -q
```

- [ ] **步骤 6：提交功能完成门禁**

```bash
git add scripts/feature_governance.py scripts/feature-finish.sh scripts/quality-gate.sh backend/tests/unit/test_feature_governance.py
git commit -m "feat: gate feature completion on tested commits"
```

### 任务 5：候选清单、顺序集成与失败停止

**文件：**
- 修改：`scripts/feature_governance.py`
- 新建：`scripts/release-prepare.sh`
- 新建：`scripts/release-integrate.sh`
- 修改：`backend/tests/unit/test_feature_governance.py`

**接口：**
- `ReleaseManifest`、`ReleaseService.prepare(version, feature_ids)`
- `ReleaseService.integrate(manifest_path, approval_token)`
- 候选清单：`<git-common-dir>/audio-memory-governance/releases/<version>-candidate.json`

- [ ] **步骤 1：先写只有 ready 且 SHA 一致的功能进入候选清单的测试**

```python
def test_prepare_lists_only_selected_ready_commits(git_fixture):
    git_fixture.ready("one")
    git_fixture.in_progress("two")
    manifest = git_fixture.release.prepare("v0.1.0-beta.3", ["one"])
    assert [item.feature_id for item in manifest.features] == ["one"]
    with pytest.raises(GovernanceError):
        git_fixture.release.prepare("v0.1.0-beta.3", ["two"])
```

- [ ] **步骤 2：先写第二个功能失败后不处理第三个功能的测试**

```python
def test_integration_stops_at_first_failed_feature(git_fixture):
    manifest = git_fixture.ready_manifest("one", "two", "three")
    result = git_fixture.release.integrate(manifest, gates={"two": False})
    assert result.merged == ("one",)
    assert result.failed == "two"
    assert not git_fixture.is_merged("three")
```

- [ ] **步骤 3：先写没有显式授权时集成命令只读的测试**

```python
def test_integrate_without_exact_approval_does_not_change_main(git_fixture):
    before = git_fixture.main_head()
    with pytest.raises(GovernanceError, match="确认"):
        git_fixture.release.integrate(git_fixture.manifest, approval_token=None)
    assert git_fixture.main_head() == before
```

- [ ] **步骤 4：实现原子候选清单与逐项集成编排**

清单包含创建时的 `main` SHA、目标版本、有序功能列表及每项已测试 SHA。集成前重新校验所有不变量；任一不匹配就在更改 `main` 前停止。每项合并后执行门禁；失败时停止并将该功能标记回 `in_progress`。

- [ ] **步骤 5：在临时仓库中运行成功、冲突、门禁失败和过期清单测试**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py -q
```

- [ ] **步骤 6：提交发布候选与集成编排**

```bash
git add scripts/feature_governance.py scripts/release-prepare.sh scripts/release-integrate.sh backend/tests/unit/test_feature_governance.py
git commit -m "feat: integrate approved feature manifests"
```

### 任务 6：发布构建双重授权与版本不可变性

**文件：**
- 修改：`scripts/feature_governance.py`
- 新建：`scripts/release-build.sh`
- 修改：`scripts/build-release.sh`
- 修改：`backend/tests/unit/test_feature_governance.py`
- 修改：`backend/tests/unit/test_release_package.py`
- 修改：`backend/tests/unit/test_release_version.py`

**接口：**
- `ReleaseService.authorize_build(manifest, approval_token)`
- `release-build.sh <version> --approve <candidate-digest>`

- [ ] **步骤 1：先写非 main、脏工作区、版本不一致、标签已存在和缺少第二授权的失败测试**

```python
@pytest.mark.parametrize("fault", ["not_main", "dirty", "wrong_version", "tag_exists", "no_approval"])
def test_release_build_refuses_unapproved_or_mutable_candidate(release_fixture, fault):
    release_fixture.apply_fault(fault)
    before = release_fixture.refs()
    assert release_fixture.run_build().returncode != 0
    assert release_fixture.refs() == before
```

- [ ] **步骤 2：运行目标测试并确认当前构建脚本没有候选清单授权**

```bash
cd backend
.venv/bin/pytest tests/unit/test_feature_governance.py tests/unit/test_release_package.py tests/unit/test_release_version.py -q
```

- [ ] **步骤 3：实现候选摘要、二次授权和发布前置校验**

授权值必须是候选清单规范 JSON 的 SHA-256，任何清单更改都使授权失效。校验通过后才调用现有 `build-release.sh`；只在成功构建和安装边界验证后创建版本标签。

- [ ] **步骤 4：验证发布包不携带 `audio-memory-governance`、worktree 或开发运行数据**

```bash
cd backend
.venv/bin/pytest tests/unit/test_release_package.py tests/unit/test_release_version.py tests/unit/test_feature_governance.py -q
```

- [ ] **步骤 5：提交发布授权门禁**

```bash
git add scripts/feature_governance.py scripts/release-build.sh scripts/build-release.sh backend/tests/unit/test_feature_governance.py backend/tests/unit/test_release_package.py backend/tests/unit/test_release_version.py
git commit -m "feat: require immutable release approval"
```

### 任务 7：仓库规则、CI 门禁与端到端验收

**文件：**
- 新建：`AGENTS.md`
- 新建：`.github/workflows/quality-gate.yml`
- 新建：`docs/qa/2026-08-19-feature-release-governance-acceptance.md`
- 修改：`backend/tests/unit/test_feature_governance.py`
- 修改：`backend/tests/e2e/test_prompt_eval_contract.py`

**接口：**
- CI 必需检查名：`backend`、`frontend`、`browser`、`runtime-isolation`
- 仓库代理规则引用统一脚本，不复制状态判定逻辑

- [ ] **步骤 1：先写仓库契约测试**

```python
def test_repository_governance_contract_names_all_required_gates():
    agents = (REPOSITORY_ROOT / "AGENTS.md").read_text()
    workflow = (REPOSITORY_ROOT / ".github/workflows/quality-gate.yml").read_text()
    for name in ("backend", "frontend", "browser", "runtime-isolation"):
        assert name in agents
        assert name in workflow
```

- [ ] **步骤 2：运行契约测试并确认根规则和 CI 尚未存在**

```bash
cd backend
.venv/bin/pytest tests/e2e/test_prompt_eval_contract.py -q
```

- [ ] **步骤 3：编写根 `AGENTS.md` 和 CI**

`AGENTS.md` 必须用中文说明新功能、继续功能、集成、发布和清理的授权边界。CI 调用 `quality-gate.sh`，不在 YAML 里维护第二套测试清单。

- [ ] **步骤 4：在临时仓库中执行双功能、跨对话恢复和第二项失败演练**

验收记录只保存临时仓库路径、虚构功能名、命令结果和不变量；不读取或修改真实 beta.2 数据。

- [ ] **步骤 5：运行全量回归**

```bash
cd backend
.venv/bin/pytest -q
cd ../prototype
node --test tests/*.test.mjs
npm run build
npm run test:e2e
```

如果沙箱禁止绑定临时本机端口，仅对需要本机端口的测试请求授权后重运，不改测试期望。

- [ ] **步骤 6：检查开发版本实际运行身份**

```bash
./scripts/feature-status.sh beta3-stability
curl -sS http://127.0.0.1:8766/api/health
```

确认开发页面为 5173、后端 profile 为 `development`，且未发生对 8765 或正式数据根的写入。

- [ ] **步骤 7：提交仓库治理与验收证据**

```bash
git add AGENTS.md .github/workflows/quality-gate.yml docs/qa/2026-08-19-feature-release-governance-acceptance.md backend/tests/unit/test_feature_governance.py backend/tests/e2e/test_prompt_eval_contract.py
git commit -m "docs: enforce the feature release workflow"
```

### 任务 8：当前 beta3-stability 轨道的审计式纳管

**文件：**
- 新建（不参与 Git 提交）：`<git-common-dir>/audio-memory-governance/features/beta3-stability.json`
- 修改：`docs/qa/2026-08-19-feature-release-governance-acceptance.md`

**前置条件：** 只读审计确认当前 worktree、`codex/beta3-stability` 分支、HEAD 和开发运行根的映射正确。

- [ ] **步骤 1：使用 `feature-status` 的审计模式生成拟纳管预览**

```bash
./scripts/feature-status.sh --adopt-preview beta3-stability
```

预期：只输出将要记录的分支、worktree、HEAD、目标版本和状态，不写文件。

- [ ] **步骤 2：向用户展示精确纳管映射并获得明确确认**

未获得确认前停止，不创建 `audio-memory-governance/features/beta3-stability.json`。

- [ ] **步骤 3：确认后原子写入当前轨道状态并重新校验**

```bash
./scripts/feature-status.sh --adopt beta3-stability --approve <preview-digest>
./scripts/feature-status.sh beta3-stability
```

- [ ] **步骤 4：提交纳管记录**

```bash
git add docs/qa/2026-08-19-feature-release-governance-acceptance.md
git commit -m "chore: enroll the beta3 stability track"
```

## 完成标准

- 一个新功能可以在不要求用户重复指定分支和环境的情况下创建。
- 同一功能可以在全新对话中仅凭 `feature_id` 恢复。
- 开发页面始终是 5173，8766 只是 development 后端。
- 多功能只能在已批准清单下顺序集成，任一失败立即停止。
- 功能修复发生在功能轨道，不直接发生在 `main`。
- 集成授权不等于发布授权；发布必须有第二次明确确认。
- 全量自动测试和临时仓库手工验收全部通过。
- beta.2 安装版本、进程、数据、密钥和日志不受影响。
