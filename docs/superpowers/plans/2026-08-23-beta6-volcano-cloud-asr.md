# Beta 6 火山云端 ASR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 Beta 5 正式报告链路的前提下，为 Beta 6 新上传任务增加产品托管阿里 OSS 临时中转、火山标准版 ASR、双配置上传门禁、文件级重试和重启续跑。

**Architecture:** 分析模型配置与 ASR 配置保持两个独立 bounded context。最小 OSS 授权服务签发单对象、短时效权限，客户端直传私有 Bucket 且不持有产品长期 AccessKey。新的持久化 `AsrCoordinator` 以火山为本期唯一 provider，负责 OSS 对象生命周期、火山提交、轮询、结果规范化和恢复，并继续通过现有 `TranscriptionService`/`Transcript` 契约向 `SingleReportRunner` 提供逐字稿；UI 只编排配置、上传与统一恢复动作。

**Tech Stack:** Python 3.12、FastAPI、httpx、SQLAlchemy/Alembic、SQLite、macOS Keychain、React 19、Vite、Node test runner、Playwright。

**Spec:** `docs/superpowers/specs/2026-08-23-beta6-volcano-cloud-asr-design.md`

## Global Constraints

- 这是 Beta 6 的一个功能模块，不代表 Beta 6 全部范围。
- 实施基线必须是 tag `v0.1.0-beta.5`，不得直接在当前脏工作树开发。
- 使用 `superpowers:using-git-worktrees` 创建 `codex/` 前缀的隔离分支与 worktree。
- 只运行开发环境；不得写入正式端口、正式数据库、正式数据目录或正式 Keychain service。
- ASR 仅支持火山，资源 ID 固定为 `volc.seedasr.auc`；用户不配置 OSS/TOS。
- 产品 OSS 长期 AccessKey 不得进入客户端代码、安装包、本地 Keychain、SQLite 或日志；没有可用的一次性授权服务时不得发布。
- 上传格式仅 MP3/AAC；单文件小于 5 小时且小于 512MB 时原文件直传，超限直接拒绝；Beta 6 不自动切分或转码。
- ASR 与分析远程调用均为首次调用加最多 2 次自动重试。
- 已确认成功的分片和分析检查点不得重复调用。
- 复用 Beta 5 `SingleReportRunner` 正式报告链路，不创建第二套报告实现。
- 不合并、不打标签、不构建发布包、不推送用户，除非用户另行授权。

---

### Task 1: 创建 Beta 6 隔离基线并锁定开发环境

**Files:**
- Modify: `backend/tests/integration/test_runtime_isolation.py`
- Modify: `backend/tests/unit/test_start_script.py`

**Interfaces:**
- Consumes: tag `v0.1.0-beta.5` and existing `RuntimeConfig` development profile.
- Produces: a clean worktree on `codex/beta6-volcano-asr` whose runtime assertions reject production paths and Keychain service.

- [ ] **Step 1: 创建隔离 worktree**

先完整读取 `superpowers:using-git-worktrees`，然后从 `v0.1.0-beta.5` 创建新 worktree 和 `codex/beta6-volcano-asr` 分支。不得 stash、reset、clean 或移动当前工作树中的用户改动。

- [ ] **Step 2: 写失败的开发隔离测试**

增加相对断言，确保 development profile 的端口、数据根和 Keychain service 均不等于由 production profile 生成的对应值。测试中显式把任一 production 值注入 development config，并断言 `RuntimeConfig.validate()` 拒绝；不把 `8765` 等具体值作为唯一安全条件。

```python
def test_development_profile_rejects_production_keychain_service(tmp_path):
    config = development_runtime(tmp_path, keychain_service="Audio Memory")
    with pytest.raises(ValueError, match="Keychain"):
        config.validate()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/integration/test_runtime_isolation.py tests/unit/test_start_script.py -v`

Expected: 新增 production Keychain boundary 用例失败。

- [ ] **Step 4: 最小化补强现有 RuntimeConfig 校验**

只在现有运行配置模块中补充开发 Keychain namespace 约束，不引入新的环境配置系统。

- [ ] **Step 5: 运行测试并提交**

Run: `cd backend && uv run pytest tests/integration/test_runtime_isolation.py tests/unit/test_start_script.py -v`

Expected: PASS。

Commit: `test: lock beta6 work to development runtime`

---

### Task 2: 用真实开发配置验证 OSS 与火山接口前提

**Files:**
- Create: `docs/benchmark-evidence/2026-08-23-volcano-api-capability-probe.md`
- Create: `backend/experiments/volcano_api_capability_probe.py`
- Create: `backend/experiments/oss_upload_capability_probe.py`

**Interfaces:**
- Consumes: development-only Volcano API Key、固定资源 ID `volc.seedasr.auc`、开发 OSS Bucket 与产品内置的极短 MP3 探针。
- Produces: 已验证的 OSS 限权上传/签名读取/删除、火山认证、request ID、提交后查询和限流契约；后续编码必须以该证据为准。

- [ ] **Step 1: 编写只使用开发环境的探针脚本**

脚本从开发密钥存储读取凭证，不接受命令行明文 Key，不打印 header、签名 URL 或正文。先验证 OSS 单对象上传、签名 URL 可被外部下载、删除与 24 小时生命周期；再记录火山的独立认证能力、稳定错误码、task ID、同一 request ID 重复提交行为、查询能力和限流信息。

- [ ] **Step 2: 先执行单次极短音频验证**

该调用可能产生极少量识别费用，执行前再次确认使用的是 development Keychain service。只提交产品内置的极短测试音频，不使用用户长录音。

- [ ] **Step 3: 受控验证重复 request ID**

在用户已经同意可能产生第二次极少量费用后，对同一探针音频重复一次 request ID。比较 HTTP 状态、task ID 和账号调用记录；不得仅根据字段名推断幂等。

- [ ] **Step 4: 固化结论和降级状态机**

若官方不提供幂等：将“提交已发出但 task ID 未确认”定义为 `submission_unknown`，禁止自动重复提交，必须先查询或要求用户确认。若无独立认证接口：保存 Key 时使用极短探针并显示可能产生极少量用量。

- [ ] **Step 5: 审阅通过后再进入 ASR 核心编码**

Expected: 文档列出真实请求次数、稳定错误码、是否幂等、查询能力与账号并发限制；不包含 Key、音频正文或未脱敏响应。

Commit: `docs: verify volcano asr api capabilities`

---

### Task 3: 建立独立的火山 ASR 配置与 Keychain 契约

**Files:**
- Create: `backend/src/audio_memory/asr/__init__.py`
- Create: `backend/src/audio_memory/asr/types.py`
- Create: `backend/src/audio_memory/asr/credentials.py`
- Create: `backend/src/audio_memory/api/asr.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/providers/keychain.py`
- Create: `backend/tests/unit/asr/test_validation.py`
- Create: `backend/tests/integration/test_asr_api.py`
- Modify: `backend/tests/e2e/test_secret_absence.py`

**Interfaces:**
- Consumes: `KeychainRepository`, local configuration session security, `httpx.AsyncClient`.
- Produces: `AsrState`, `AsrValidationResult`, `AsrCredentialCoordinator`, and endpoints `GET /api/asr`, `PUT /api/asr/key`, `POST /api/asr/validate`.

- [ ] **Step 1: 写类型和 API 契约测试**

测试响应只包含状态，不包含 Key，并固定返回 provider/resource：

```python
assert response.json() == {
    "provider_id": "volcano",
    "display_name": "火山语音",
    "resource_id": "volc.seedasr.auc",
    "state": "unconfigured",
    "last_validated_at": None,
    "error_code": None,
    "error_code": None,
}
```

同时覆盖 candidate 校验成功后替换旧 Key、失败不覆盖、日志和 SQLite 不含明文 Key。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/asr/test_validation.py tests/integration/test_asr_api.py tests/e2e/test_secret_absence.py -v`

Expected: import 或路由不存在而 FAIL。

- [ ] **Step 3: 实现独立配置类型**

在 `asr/types.py` 定义：

```python
class AsrProviderId(StrEnum):
    VOLCANO = "volcano"

ASR_PROVIDER_CONFIGS = {
    AsrProviderId.VOLCANO: AsrProviderConfig(
        resource_id="volc.seedasr.auc",
        max_duration_ms=5 * 60 * 60 * 1000,
        max_size_bytes=512 * 1024 * 1024,
    )
}

class AsrStateValue(StrEnum):
    UNCONFIGURED = "unconfigured"
    VALIDATING = "validating"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    KEYCHAIN_UNAVAILABLE = "keychain_unavailable"
```

ASR coordinator 不得加入 `PROVIDER_CONFIGS`，避免与分析模型的 active provider 语义混淆。

- [ ] **Step 4: 实现候选 Key 校验与提交**

`AsrCredentialCoordinator.validate_candidate(session_id: str, candidate: bytes) -> AsrValidationResult` 必须按 Task 2 验证出的官方能力校验，成功后才写 Keychain；若无独立认证接口则使用极短探针并返回用量说明。Keychain account 使用独立 account `asr_provider_volcano`，service 从 `RuntimeConfig.keychain_service` 派生。

- [ ] **Step 5: 注册 API 与生命周期资源**

`main.py` 创建专用短超时 validation client，注册 router，并在 shutdown 关闭 client。任何错误响应只返回稳定错误码和中文行动建议。

- [ ] **Step 6: 运行测试并提交**

Run: `cd backend && uv run pytest tests/unit/asr/test_validation.py tests/integration/test_asr_api.py tests/e2e/test_secret_absence.py -v`

Expected: PASS。

Commit: `feat: add isolated volcano asr configuration`

---

### Task 4: 增加双配置 readiness 与上传前硬门禁

**Files:**
- Create: `backend/src/audio_memory/readiness.py`
- Create: `backend/src/audio_memory/api/readiness.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `backend/src/audio_memory/uploads/service.py`
- Modify: `backend/src/audio_memory/main.py`
- Create: `backend/tests/integration/test_upload_readiness.py`
- Modify: `backend/tests/integration/test_upload_jobs.py`

**Interfaces:**
- Consumes: `ProviderStateCoordinator.snapshot_active_with_generation()` and `AsrCredentialCoordinator.state()`.
- Produces: `PipelineReadiness.check() -> PipelineReadinessView` and `GET /api/readiness`.

- [ ] **Step 1: 写四种 readiness 组合测试**

覆盖 analysis/asr 的 TT、TF、FT、FF。不可用时：

```python
assert response.status_code == 409
assert response.json()["detail"]["code"] == "configuration_required"
assert response.json()["detail"]["missing"] == ["asr:volcano"]
assert upload_stream.bytes_read == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/integration/test_upload_readiness.py tests/integration/test_upload_jobs.py -v`

Expected: job 仍会创建或上传流被读取。

- [ ] **Step 3: 实现 readiness 聚合**

```python
@dataclass(frozen=True, slots=True)
class PipelineReadinessView:
    ready: bool
    analysis_ready: bool
    asr_ready: bool
    missing: tuple[str, ...]
```

`check(now: datetime)` 默认只读取本地状态和 `last_validated_at`。只有尚未验证、状态为 `unavailable` 或超过配置有效期时，才并发主动校验两类配置；同一次多文件上传只生成一个 readiness snapshot，不能按文件重复远程校验。

- [ ] **Step 4: 在读取文件前拒绝**

jobs router 在调用 `UploadService.upload()` 前完成 readiness 检查。对 `POST /api/jobs` 同样检查，避免留下空 job。不能依赖前端禁用态作为安全边界。

- [ ] **Step 5: 运行测试并提交**

Run: `cd backend && uv run pytest tests/integration/test_upload_readiness.py tests/integration/test_upload_jobs.py -v`

Expected: PASS。

Commit: `feat: gate uploads on analysis and asr readiness`

---

### Task 5: 持久化 ASR 文件任务与迁移

**Files:**
- Modify: `backend/src/audio_memory/models.py`
- Modify: `backend/src/audio_memory/repositories.py`
- Create: `backend/src/audio_memory/asr/repository.py`
- Create: `backend/migrations/versions/<revision>_add_volcano_asr_tasks.py`
- Create: `backend/tests/integration/test_asr_migration.py`
- Create: `backend/tests/unit/asr/test_repository.py`

**Interfaces:**
- Consumes: existing `AnalysisJob`, `JobFile`, `Transcript`.
- Produces: `AsrFileTask`, `AsrRepository.claim_due_files()`, `mark_submitted()`, `mark_submission_unknown()`, `mark_completed()`, `mark_retry_wait()`, `mark_failed()`.

- [ ] **Step 1: 写迁移与仓储失败测试**

断言从 Beta 5 schema 升级后新增文件任务表，并保证一个上传文件只有一个 ASR 任务：

```python
UniqueConstraint("job_file_id", name="uq_asr_file_task")
UniqueConstraint("request_id", name="uq_asr_request_id")
```

并验证重复 complete、重复 transcript materialization 均为 no-op。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/integration/test_asr_migration.py tests/unit/asr/test_repository.py -v`

Expected: 表或模型不存在。

- [ ] **Step 3: 实现最小持久化模型**

`AsrFileTask` 保存 `relative_source_path/sha256/request_id/storage_object_id/storage_status/remote_task_id/status/attempt_count/next_attempt_at/error_code/result_json/materialized_at`。`storage_object_id` 是脱敏随机对象标识，不存储签名 URL。路径必须相对于运行环境数据根；数据库不保存供应商原始错误消息、Key、HTTP header 或未脱敏 response。

- [ ] **Step 4: 实现原子状态转换**

repository 方法以条件 UPDATE 保证 lease/claim 和状态推进原子；只有 `completed` result 才允许 materialize。task ID 必须在提交响应返回后立即提交事务。

- [ ] **Step 5: 运行迁移与仓储测试并提交**

Run: `cd backend && uv run pytest tests/integration/test_asr_migration.py tests/unit/asr/test_repository.py tests/integration/test_database_schema.py -v`

Expected: PASS。

Commit: `feat: persist resumable asr file tasks`

---

### Task 6: 实现原文件约束校验和逐字稿规范化

**Files:**
- Create: `backend/src/audio_memory/asr/files.py`
- Create: `backend/src/audio_memory/asr/normalizer.py`
- Modify: `backend/src/audio_memory/transcription/segments.py`
- Create: `backend/tests/unit/asr/test_files.py`
- Create: `backend/tests/unit/asr/test_normalizer.py`

**Interfaces:**
- Consumes: probed `JobFile` metadata and provider limits from `ASR_PROVIDER_CONFIGS`.
- Produces: validated original-file submission and normalized `list[TranscriptSegment]` with original timestamps.

- [ ] **Step 1: 写原文件直传与超限拒绝测试**

```python
def test_supported_small_mp3_is_not_transcoded():
    submission = validate_asr_file(file, config)
    assert submission.relative_path == file.relative_path
    assert submission.transcoded is False

@pytest.mark.parametrize("duration_ms,size_bytes", [
    (5 * 60 * 60 * 1000, 1),
    (1, 512 * 1024 * 1024),
])
def test_limit_is_rejected_before_remote_submission(duration_ms, size_bytes):
    with pytest.raises(AsrFileError, match="file_exceeds_asr_limit"):
        validate_asr_file(make_file(duration_ms, size_bytes), config)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/asr/test_files.py tests/unit/asr/test_normalizer.py -v`

Expected: modules 不存在。

- [ ] **Step 3: 实现原文件校验**

服务限制集中在 provider 配置。校验扩展名、探测格式、时长、大小、相对路径边界和 sha256；合格文件返回原文件引用，超限返回 `file_exceeds_asr_limit`，不调用 ffmpeg、转码或切分。

- [ ] **Step 4: 实现结果规范化和幂等写入**

把火山句段直接转换为现有 `TranscriptSegment`，校验时间戳单调、范围不超过文件时长，并重新分配稳定 segment index。重复 materialize 必须为 no-op。

- [ ] **Step 5: 运行测试并提交**

Run: `cd backend && uv run pytest tests/unit/asr/test_files.py tests/unit/asr/test_normalizer.py -v`

Expected: PASS。

Commit: `feat: validate direct asr files and normalize results`

---

### Task 7: 实现火山提交、轮询、重试与重启恢复

**Files:**
- Create: `backend/src/audio_memory/asr/storage.py`
- Create: `backend/src/audio_memory/asr/client.py`
- Create: `backend/src/audio_memory/asr/coordinator.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Create: `backend/tests/unit/asr/test_client.py`
- Create: `backend/tests/integration/test_cloud_asr_pipeline.py`
- Create: `backend/tests/integration/test_cloud_asr_recovery.py`

**Interfaces:**
- Consumes: `AsrRepository`, OSS temporary-authorization client, planner, normalizer, ASR Keychain credential, `AnalysisTaskCoordinator.submit_new_upload()`.
- Produces: `VolcanoAsrCoordinator.run_job(job_id)`, `resume_job(job_id)`, and startup `AsrRecoveryService.resume_incomplete()`.

- [ ] **Step 1: 写 OSS 权限和客户端错误分类测试**

先断言客户端只能获取限定 object key 的短期上传权限，不能列举 Bucket、覆盖其他对象或读取长期 AccessKey。再参数化覆盖 timeout/429/5xx 为 retryable，401/403/quota/invalid format 为 terminal，并断言总尝试次数最大为 3：

```python
assert fake_transport.calls_for(file_task.id) == 3
assert await repository.status(file_task.id) == "failed"
```

- [ ] **Step 2: 写恢复测试**

覆盖：已有 task ID 只 poll、不 resubmit；completed 文件不调用；进程在 materialize 前退出后只 materialize；全部完成后只提交一个 analysis version；`submission_unknown` 在无官方查询能力时不自动重提交。

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/asr/test_client.py tests/integration/test_cloud_asr_pipeline.py tests/integration/test_cloud_asr_recovery.py -v`

Expected: FAIL。

- [ ] **Step 4: 实现 OSS 临时存储 client 和火山 client**

storage client 只调用产品授权服务的“获取单对象上传权限/获取短时读 URL/删除”契约，不接收 OSS 长期 AccessKey。火山 client 只接收 `api_key`、provider 配置、短时读 URL 和稳定 request ID。`submit()` 返回 task ID；`poll()` 返回 pending/completed/result。request ID 只按 Task 2 的真实证据实现，不宣称未验证的幂等。日志通过现有 observability helper 记录脱敏事件。

- [ ] **Step 5: 实现 coordinator 与退避**

每个文件的 OSS 上传和 ASR 最多各 3 次总尝试，成功状态不可回退。已上传对象不重复上传，已提交 task ID 不重复提交。提交队列用 semaphore 将并发固定为 2，轮询使用独立队列；429 优先遵循 `Retry-After`，否则指数退避，本地排队不增加 attempt。全部 materialized 后调用现有 risk/safety 必要的结构校验，但不得启动本地 Whisper；然后推进 `analyzing` 并提交现有 `AnalysisRequest`。OSS 删除失败进入独立清理队列，不重复 ASR。

- [ ] **Step 6: 接入启动恢复**

应用启动后由 coordinator 扫描未完成 ASR 状态并排队恢复，不再创建只有一层转发的 recovery 模块。不要把所有 `transcribing` job 一律标记为 interrupted；云端 task 有 ID 时恢复 polling，`submission_unknown` 按 Task 2 结论处理。shutdown 只取消本地 worker，不删除持久化状态。

- [ ] **Step 7: 运行测试并提交**

Run: `cd backend && uv run pytest tests/unit/asr/test_client.py tests/integration/test_cloud_asr_pipeline.py tests/integration/test_cloud_asr_recovery.py tests/integration/test_transcription_recovery.py -v`

Expected: PASS。

Commit: `feat: run and recover volcano cloud transcription`

---

### Task 8: 补强正式分析链路的两次重试与检查点幂等

**Files:**
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Modify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Modify: `backend/src/audio_memory/analysis/publisher.py`
- Modify: `backend/src/audio_memory/analysis/errors.py`
- Modify: `backend/tests/integration/test_audited_single_report_runner.py`
- Modify: `backend/tests/unit/analysis/test_task_coordinator.py`
- Create: `backend/tests/integration/test_analysis_checkpoint_resume.py`

**Interfaces:**
- Consumes: existing `pipeline_checkpoints_json`, existing Beta 5 report phases.
- Produces: one shared `call_with_retry(stage, operation, max_retries=2)` policy and idempotent resume for generation/audit chunks/merge/revision/final audit/publication.

- [ ] **Step 1: 写逐阶段失败矩阵测试**

每个阶段分别在第 1、2 次失败和第 3 次失败；断言前两类最终成功，第三类进入 failed，且已完成阶段调用次数保持 1。分区审计只增加失败分区的调用次数。

- [ ] **Step 2: 写发布响应丢失测试**

模拟 publisher 已写入 report 后抛连接错误；resume 必须查询现有 publication 并返回已有结果，不新建第二张卡或第二个 analysis version。

- [ ] **Step 3: 运行测试确认现有缺口**

Run: `cd backend && uv run pytest tests/integration/test_audited_single_report_runner.py tests/integration/test_analysis_checkpoint_resume.py tests/unit/analysis/test_task_coordinator.py -v`

Expected: 至少一个重试次数或幂等恢复用例失败。

- [ ] **Step 4: 统一重试策略并在成功后立即写检查点**

只对网络、429、5xx 等稳定分类重试；认证/余额/内容契约错误立即失败。每个远程结果先原子持久化再推进下一阶段。

- [ ] **Step 5: 运行测试并提交**

Run: `cd backend && uv run pytest tests/integration/test_audited_single_report_runner.py tests/integration/test_analysis_checkpoint_resume.py tests/unit/analysis/test_task_coordinator.py -v`

Expected: PASS。

Commit: `fix: resume formal analysis from confirmed checkpoints`

---

### Task 9: 实现配置、门禁、进度和防休眠交互

**Files:**
- Modify: `prototype/src/api/client.js`
- Modify: `prototype/src/api/state.js`
- Create: `prototype/src/hooks/useAsr.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/src/styles.css`
- Modify: `prototype/tests/api-client.test.mjs`
- Create: `prototype/tests/asr-state.test.mjs`
- Modify: `prototype/tests/product-state.test.mjs`
- Modify: `prototype/tests/e2e/first-run.spec.js`
- Modify: `prototype/tests/e2e/recovery.spec.js`
- Modify: `prototype/tests/e2e/sleep-prevention.spec.js`
- Create: `prototype/tests/e2e/cloud-asr-flow.spec.js`

**Interfaces:**
- Consumes: `/api/asr`, `/api/readiness`, enriched `JobView` progress/error fields, existing settings API.
- Produces: non-technical Beta 6 UI for Volcano API configuration, pre-upload gate, start notice, switch-off notice, cloud progress and unified continue action.

- [ ] **Step 1: 写前端状态和 API 失败测试**

断言 `normalizeAsr()` 不保存 Key；`addFiles()` 在 readiness false 时不调用 createJob/upload；配置弹窗只显示 API Key，不出现 TOS、resource ID 输入或模型选择。

- [ ] **Step 2: 写 Playwright 用户旅程**

覆盖：双配置未完成、完成后上传、开始后单按钮说明、关闭网页后后台任务继续、重载恢复进度、失败后继续、关闭防休眠开关后的单按钮提示。

防休眠提示必须断言：

```javascript
await switchControl.uncheck()
await expect(page.getByText('请尽量保持开启。电脑自动休眠会暂停转写或分析；唤醒后将从最近完成的步骤继续。')).toBeVisible()
await expect(page.getByRole('button', { name: '知道了' })).toHaveCount(1)
await expect(switchControl).not.toBeChecked()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd prototype && node --test tests/api-client.test.mjs tests/asr-state.test.mjs tests/product-state.test.mjs`

Run: `cd prototype && npx playwright test tests/e2e/first-run.spec.js tests/e2e/recovery.spec.js tests/e2e/sleep-prevention.spec.js tests/e2e/cloud-asr-flow.spec.js`

Expected: 新流程用例 FAIL。

- [ ] **Step 4: 实现语音 API 卡片和上传门禁**

分析配置和语音配置并列但不混用。选择文件前请求 readiness；失败时打开配置视图并聚焦缺失项。后端 409 仍需映射为相同 UI，处理配置在选择后失效的竞争条件。

- [ ] **Step 5: 实现状态文案和统一恢复**

`JobPanel` 根据 ASR 细分阶段显示提交/云端转写/整理逐字稿；“继续任务”调用一个后端 resume endpoint，由后端决定 ASR 或分析恢复。

- [ ] **Step 6: 实现两个单按钮提示**

开始任务后展示关闭/休眠说明，只有“知道了”。防休眠开关从开到关且有活动任务时展示关闭提醒，也只有“知道了”，不回滚开关值。移除 Beta 5 开始前的双选择防休眠弹窗，开始任务不以开启防休眠为前置条件。

- [ ] **Step 7: 更新隐私文案**

明确说明：“音频会临时上传至 Audio Memory 管理的阿里云 OSS，供火山语音转写，转写完成后删除；转写文本发送至当前分析模型。本机保留原始音频、逐字稿和报告副本。”

- [ ] **Step 8: 运行测试并提交**

Run: `cd prototype && node --test tests/*.test.mjs`

Run: `cd prototype && npx playwright test tests/e2e/first-run.spec.js tests/e2e/recovery.spec.js tests/e2e/sleep-prevention.spec.js tests/e2e/cloud-asr-flow.spec.js`

Expected: PASS。

Commit: `feat: add volcano asr setup and recovery experience`

---

### Task 10: 开发环境真实验证与 Beta 6 功能交付证据

**Files:**
- Create: `docs/benchmark-evidence/2026-08-23-beta6-volcano-asr-acceptance.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: four July 31 MP3 test files already selected by the user, one AAC fixture, synthetic over-limit metadata cases, development-only Volcano key, development analysis key.
- Produces: repeatable acceptance evidence without publication or formal-environment mutation.

- [ ] **Step 1: 运行完整自动化回归**

Run: `cd backend && uv run pytest -q`

Run: `cd prototype && node --test tests/*.test.mjs`

Run: `cd prototype && npx playwright test`

Expected: 全部 PASS；如存在基线失败，单独记录且不能把它误报为本功能通过。

- [ ] **Step 2: 启动隔离开发环境并核对边界**

运行项目既有 development start 命令，确认 health 返回 development profile，并逐项比较 development 与 production profile：端口、数据库路径、数据根和 Keychain service 均不相同。记录核对结果，不打印 Key。

- [ ] **Step 3: 用四个 MP3 和一个 AAC 执行真实链路**

四个 MP3 上传顺序保持用户给定任务顺序，并增加一个有明确参考文本的 AAC。记录每个文件时长、提交耗时、云端完成耗时、总转写耗时、报告分析耗时、重试次数和最终状态；人工回听抽查关键名词。另用构造的 5 小时边界和 512MB 边界元数据验证发送字节前拒绝，不创建超大实体测试文件。

- [ ] **Step 4: 执行故障恢复演练**

分别在 ASR polling 和分析阶段停止开发服务，再启动；验证同一 task ID/检查点继续、成功调用计数不增加。断网测试确认恢复后继续。关闭网页测试确认后台任务未中断。

- [ ] **Step 5: 写验收证据和用户说明**

验收文档按“环境证据、自动化结果、真实任务结果、恢复演练、已知限制、正式环境未触碰证明”组织。README 只添加 Beta 6 候选功能说明，不修改正式版本号。

- [ ] **Step 6: 最终验证与提交**

重新运行受影响测试并检查 `git diff --check`、secret scan、开发/正式路径差异。不得创建 Beta 6 tag。

Commit: `docs: record beta6 volcano asr acceptance`

---

## Plan Self-Review Checklist

- [x] Spec 中的双配置、格式、TOS 边界、原文件直传、超限拒绝、重试、恢复、交互、隐私与环境隔离均映射到至少一个 Task。
- [x] 新增 ASR 类型没有混入分析 `PROVIDER_CONFIGS`。
- [x] `VolcanoAsrCoordinator` 输出仍为现有 `TranscriptSegment`/`Transcript`，正式报告入口未改变。
- [x] 所有 retry 数量统一为“首次调用 + 最多 2 次重试”，总尝试最多 3 次。
- [x] 前后端都执行上传门禁，且后端在读取文件 body 前拒绝。
- [x] 所有恢复路径都先查已确认状态，再决定调用远端；`submission_unknown` 不会在幂等能力未证实时自动重提交。
- [x] 没有任何步骤授权合并、打 tag、构建发布包或推送用户。
