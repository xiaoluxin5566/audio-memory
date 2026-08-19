# Audio Memory Dev/Production Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production and development Audio Memory instances use provably separate writable data, credentials, ports, locks, and runtime identity while preserving the published production defaults and history path.

**Architecture:** Add one immutable runtime configuration resolver in the backend and make all entry points consume its resolved profile, paths, port, and Keychain service. Development runs in the foreground with repository-local writable data and read-only shared production models; runtime health identity and the frontend prevent a development UI from mutating a production backend.

**Tech Stack:** Python 3.12, dataclasses, pathlib, FastAPI, pytest, Bash, React 19, Vite 6, Node test runner.

**Spec:** `docs/superpowers/specs/2026-08-18-dev-prod-isolation-design.md`

## Global Constraints

- Base all work on commit `0f7df7e` in `codex/dev-prod-isolation`; do not modify the original dirty worktree.
- Preserve the production defaults exactly: profile `production`, data root `~/Library/Application Support/AudioMemory`, model root `<data-root>/models`, port `8765`, Keychain service `Audio Memory`, LaunchAgent `com.audio-memory.local`.
- Development defaults are profile `development`, data root `<worktree>/.runtime/dev`, model root `~/Library/Application Support/AudioMemory/models`, port `8766`, Keychain service `Audio Memory Dev`, and no LaunchAgent.
- Never read existing Keychain secrets, call a real provider, upload real audio, or mutate the production database during implementation or verification.
- Development may read shared production models but must never create, chmod, download, update, move, or delete them.
- Do not merge, push, tag, publish, or modify the `v0.1.0-beta.1` Release.
- Every behavior change follows red-green-refactor; record the exact failing assertion before implementation.

---

### Task 1: Resolve One Typed Runtime Configuration

**Files:**
- Modify: `backend/src/audio_memory/config.py`
- Modify: `backend/tests/unit/test_config.py`

**Interfaces:**
- Produces: `AppProfile(StrEnum)`, `RuntimeConfig`, `RuntimeConfig.from_environment(*, home: Path, project_root: Path, environ: Mapping[str, str] | None = None) -> RuntimeConfig`.
- Produces: `RuntimeConfig.paths -> AppPaths`, `profile`, `port`, and `keychain_service` fields consumed by Tasks 3–6.

- [ ] **Step 1: Write failing tests for production compatibility and development defaults**

```python
def test_runtime_config_preserves_production_defaults(tmp_path: Path) -> None:
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home", project_root=tmp_path / "repo", environ={}
    )
    assert config.profile is AppProfile.PRODUCTION
    assert config.paths.root == tmp_path / "home/Library/Application Support/AudioMemory"
    assert config.paths.models == config.paths.root / "models"
    assert config.port == 8765
    assert config.keychain_service == "Audio Memory"


def test_runtime_config_uses_isolated_development_defaults(tmp_path: Path) -> None:
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "repo",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )
    assert config.paths.root == (tmp_path / "repo/.runtime/dev").resolve()
    assert config.paths.models == (
        tmp_path / "home/Library/Application Support/AudioMemory/models"
    ).resolve()
    assert config.port == 8766
    assert config.keychain_service == "Audio Memory Dev"
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `cd backend && .venv/bin/pytest tests/unit/test_config.py -q`

Expected: import failure for `AppProfile` or `RuntimeConfig`, not an unrelated setup error.

- [ ] **Step 3: Add invalid-value and explicit-override tests**

Cover unknown/case-mismatched profile, non-integer/out-of-range port, blank Keychain service, and all three explicit overrides. Assert invalid configuration raises `RuntimeConfigurationError` before directories exist.

- [ ] **Step 4: Implement the minimal resolver**

Use a frozen, slotted dataclass. Copy `os.environ` only when `environ is None`; tests pass explicit mappings. Expand and resolve paths without creating them. Keep `AppPaths.from_home()` as a compatibility wrapper for existing tests and callers.

- [ ] **Step 5: Run Task 1 tests and the existing config/health tests**

Run: `cd backend && .venv/bin/pytest tests/unit/test_config.py tests/integration/test_health.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add backend/src/audio_memory/config.py backend/tests/unit/test_config.py
git commit -m "feat: resolve audio memory runtime profiles"
```

### Task 2: Separate Writable Paths From Shared Models and Reject Dangerous Development Roots

**Files:**
- Modify: `backend/src/audio_memory/config.py`
- Modify: `backend/tests/unit/test_config.py`

**Interfaces:**
- Produces: `AppPaths.from_roots(data_root: Path, model_root: Path | None = None, *, models_writable: bool = True) -> AppPaths`.
- Produces: `RuntimeConfig.validate_development_isolation() -> None` and `UnsafeDevelopmentPathError`.
- Consumes: Task 1 `RuntimeConfig` and `AppProfile`.

- [ ] **Step 1: Write failing tests for writable-directory and model behavior**

```python
def test_development_directory_setup_never_mutates_shared_models(tmp_path: Path) -> None:
    shared = tmp_path / "production/models"
    paths = AppPaths.from_roots(
        tmp_path / "repo/.runtime/dev", shared, models_writable=False
    )
    paths.ensure_directories()
    assert paths.root.is_dir()
    assert not shared.exists()
    assert shared not in paths.required_directories
```

Add tests proving database, runtime, lock, feedback, staging, audio, prompts, and local-session path all remain below the development data root.

- [ ] **Step 2: Run and confirm RED because model writability is not represented**

Run: `cd backend && .venv/bin/pytest tests/unit/test_config.py -q`

- [ ] **Step 3: Add dangerous-path tests**

Test exact production root, a child of production root, and a symlink that resolves into production. In every case assert `UnsafeDevelopmentPathError` and assert the requested development root was not created.

- [ ] **Step 4: Implement split roots and preflight validation**

Keep external shared models out of `required_directories`. Resolve existing symlinks before `Path.is_relative_to()` comparisons. Run validation during configuration resolution, before `ensure_directories()` or migrations.

- [ ] **Step 5: Run focused and path-consuming regression tests**

Run: `cd backend && .venv/bin/pytest tests/unit/test_config.py tests/integration/test_upload_jobs.py tests/integration/test_diarization_pipeline.py -q`

- [ ] **Step 6: Commit Task 2**

```bash
git add backend/src/audio_memory/config.py backend/tests/unit/test_config.py
git commit -m "feat: isolate writable data from shared models"
```

### Task 3: Wire Runtime Identity Into the App and Keychain

**Files:**
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/providers/keychain.py`
- Modify: `backend/tests/integration/test_health.py`
- Modify: `backend/tests/unit/providers/test_keychain.py`

**Interfaces:**
- Modifies: `create_app(*, runtime_config: RuntimeConfig | None = None, paths: AppPaths | None = None, frontend_dir: Path | None = None, local_port: int | None = None) -> FastAPI`.
- Modifies: `KeychainRepository(client: SecurityClient, service: str = "Audio Memory")`.
- Produces: `GET /api/health` includes `profile` and no paths or credential identifiers.

- [ ] **Step 1: Write failing Keychain service tests**

Extend `FakeSecurityClient` to record `(service, account)` for read/update/add. Assert the default remains `Audio Memory`, and an injected `Audio Memory Dev` is used for every operation.

- [ ] **Step 2: Run Keychain tests and confirm RED on constructor signature or recorded service**

Run: `cd backend && .venv/bin/pytest tests/unit/providers/test_keychain.py -q`

- [ ] **Step 3: Implement configurable Keychain service and run GREEN**

Keep `SERVICE = "Audio Memory"` as the public compatibility default, store the selected service per repository instance, and reject blank service at `RuntimeConfig`, not inside Security calls.

- [ ] **Step 4: Update health tests first**

Change the existing exact health response assertion to include `"profile": "production"`; add a development test using an injected `RuntimeConfig` and assert the JSON keys exclude `data_root`, `model_root`, and `keychain_service`.

- [ ] **Step 5: Run health tests and confirm RED on missing profile**

Run: `cd backend && .venv/bin/pytest tests/integration/test_health.py -q`

- [ ] **Step 6: Wire `RuntimeConfig` through `create_app()`**

Resolve once when not injected. Preserve explicit `paths` and `local_port` overrides for existing tests, but keep the resolved profile and Keychain service. Store `app.state.runtime_config`; build `KeychainRepository` with its service; return the profile from health.

- [ ] **Step 7: Run app/security/provider regression tests**

Run: `cd backend && .venv/bin/pytest tests/integration/test_health.py tests/integration/test_local_web_security.py tests/integration/test_provider_api.py tests/unit/providers/test_keychain.py -q`

- [ ] **Step 8: Commit Task 3**

```bash
git add backend/src/audio_memory/main.py backend/src/audio_memory/providers/keychain.py backend/tests/integration/test_health.py backend/tests/unit/providers/test_keychain.py
git commit -m "feat: expose runtime profile and isolate keychain service"
```

### Task 4: Add Guarded Development Start and Stop Commands

**Files:**
- Create: `scripts/runtime_config.py`
- Create: `scripts/dev-start.sh`
- Create: `scripts/dev-stop.sh`
- Create: `backend/tests/unit/test_dev_scripts.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `scripts/runtime_config.py development-env --project-root PATH --home PATH`, printing shell-safe assignments only after validation.
- Produces: `scripts/dev-start.sh` and `scripts/dev-stop.sh` with development-only PID/log files under `.runtime/dev/runtime`.
- Consumes: Task 1/2 path rules; the helper imports the backend resolver rather than reimplementing comparisons in Bash.

- [ ] **Step 1: Write failing subprocess tests for resolved development variables**

Use a temporary copied project marker and temporary HOME. Invoke the helper and assert profile, data root, model root, service, and port. Add exact-root, child-root, and symlink escape rejection tests; after each rejection assert no database, lock, PID, or log exists.

- [ ] **Step 2: Run and confirm RED because helper/scripts do not exist**

Run: `cd backend && .venv/bin/pytest tests/unit/test_dev_scripts.py -q`

- [ ] **Step 3: Implement the shared helper and start script**

`dev-start.sh` evaluates only the fixed assignment names emitted by the trusted local helper, exports them, checks that health matches `development`, checks port occupancy without auto-switching, writes a PID only after spawning the expected Uvicorn command, and removes it on exit. Add `/.runtime/` to `.gitignore`.

- [ ] **Step 4: Write failing stop-safety tests**

Use fake `kill`, `curl`, and `lsof` executables placed first in `PATH`. Prove `dev-stop.sh` refuses an unrelated/stale PID, removes a stale PID record, never invokes `launchctl`, and sends TERM only when PID plus development health identity match.

- [ ] **Step 5: Implement minimal safe stop behavior**

Validate PID as numeric, verify the process command contains this worktree's backend Uvicorn entry, and verify port `8766` health reports `development` before TERM. Never use a process-name-wide kill.

- [ ] **Step 6: Run script tests and shell syntax checks**

Run: `bash -n scripts/dev-start.sh scripts/dev-stop.sh && cd backend && .venv/bin/pytest tests/unit/test_dev_scripts.py -q`

- [ ] **Step 7: Commit Task 4**

```bash
git add .gitignore scripts/runtime_config.py scripts/dev-start.sh scripts/dev-stop.sh backend/tests/unit/test_dev_scripts.py
git commit -m "feat: add guarded development lifecycle scripts"
```

### Task 5: Make Doctor Profile-Aware Without Changing Production CLI Defaults

**Files:**
- Modify: `scripts/doctor.sh`
- Modify: `scripts/audio-memory`
- Modify: `scripts/com.audio-memory.local.plist.template`
- Modify: `backend/tests/e2e/test_prompt_eval_contract.py`
- Modify: `backend/tests/unit/test_audio_memory_cli.py`

**Interfaces:**
- Consumes: Task 4 `scripts/runtime_config.py` for validated profile inputs.
- Production LaunchAgent explicitly receives `AUDIO_MEMORY_PROFILE=production`, its existing data root, and port.
- Doctor checks health JSON profile equality as well as HTTP success.

- [ ] **Step 1: Write failing doctor tests for both profiles**

Extend the existing doctor subprocess fixtures to set temporary data/model roots. Assert production reports production/8765, development reports development/8766, development accepts readable non-writable shared models, and a health payload with the wrong profile fails.

- [ ] **Step 2: Run focused doctor tests and confirm RED**

Run: `cd backend && .venv/bin/pytest tests/e2e/test_prompt_eval_contract.py -k doctor -q`

- [ ] **Step 3: Implement profile-aware doctor checks**

Resolve config through the shared helper. Print only profile, port, and data classification. Pass model root to `doctor_checks.py`; skip LaunchAgent/Keychain writability assumptions in development; parse health JSON with Python and require matching profile.

- [ ] **Step 4: Write production compatibility tests before changing CLI/template**

Assert the rendered LaunchAgent contains `AUDIO_MEMORY_PROFILE=production`, existing label and paths remain unchanged, CLI health only recognizes profile `production`, and `version/logs/status` output contracts remain stable.

- [ ] **Step 5: Update CLI/template minimally and run regression**

Run: `cd backend && .venv/bin/pytest tests/unit/test_audio_memory_cli.py tests/e2e/test_prompt_eval_contract.py -k 'doctor or audio_memory or launch_agent' -q`

- [ ] **Step 6: Commit Task 5**

```bash
git add scripts/doctor.sh scripts/audio-memory scripts/com.audio-memory.local.plist.template backend/tests/e2e/test_prompt_eval_contract.py backend/tests/unit/test_audio_memory_cli.py
git commit -m "feat: diagnose and identify runtime profiles"
```

### Task 6: Show Development Identity and Block a Misrouted Development UI

**Files:**
- Modify: `prototype/vite.config.mjs`
- Modify: `prototype/src/api/client.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/src/styles.css`
- Create: `prototype/tests/runtime-environment.test.mjs`
- Modify: `prototype/tests/dev-proxy-security.test.mjs`

**Interfaces:**
- Produces: `api.health() -> Promise<{profile: "production" | "development", ...}>`.
- Produces: exported pure helper `runtimeEnvironment(expectedProfile, healthPayload)` returning `{profile, blocked, label, message}` for unit tests.
- Development Vite config defines expected runtime profile `development` and defaults backend URL to `http://127.0.0.1:8766`.

- [ ] **Step 1: Write failing pure-state tests**

```javascript
test('development UI marks a development backend', () => {
  assert.deepEqual(runtimeEnvironment('development', { profile: 'development' }), {
    profile: 'development', blocked: false, label: '开发环境', message: '',
  })
})

test('development UI blocks a production backend', () => {
  const state = runtimeEnvironment('development', { profile: 'production' })
  assert.equal(state.blocked, true)
  assert.match(state.message, /正式环境/)
})
```

Also assert production has no label and invalid/missing profile is blocked when an expected profile is declared.

- [ ] **Step 2: Run and confirm RED because helper/health client do not exist**

Run: `cd prototype && node --test tests/runtime-environment.test.mjs`

- [ ] **Step 3: Implement runtime identity load and mutation guard**

Load `/api/health` before normal mutable workflows. Pass expected profile from `import.meta.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE`. In development mismatch state, render a blocking error and prevent session creation and every mutation; do not rely only on the banner.

- [ ] **Step 4: Update proxy tests first for port and profile behavior**

Make the fake backend expose `/api/health`. Assert the Vite default backend is `8766`, a development response proxies normally, and a production response cannot reach the fake mutation counter through the development UI boundary.

- [ ] **Step 5: Add the restrained visual marker**

Place one persistent text badge in the existing application header. Add only scoped badge/blocking-state styles; do not restructure upload, Feed, detail, history, or settings.

- [ ] **Step 6: Run frontend tests and build**

Run: `cd prototype && node --test tests/*.test.mjs && npm run build`

- [ ] **Step 7: Commit Task 6**

```bash
git add prototype/vite.config.mjs prototype/src/api/client.js prototype/src/App.jsx prototype/src/styles.css prototype/tests/runtime-environment.test.mjs prototype/tests/dev-proxy-security.test.mjs
git commit -m "feat: identify and guard the development interface"
```

### Task 7: Prove End-to-End Isolation and Preserve Release Packaging

**Files:**
- Create: `backend/tests/integration/test_runtime_isolation.py`
- Modify: `backend/tests/unit/test_release_package.py`
- Modify: `backend/tests/unit/test_release_installer.py`
- Modify: `scripts/build-release.sh` only if the failing whitelist test proves a missing required script
- Modify: `scripts/install-release.sh` only if the failing installer test proves the new production runtime files are absent
- Create: `docs/qa/2026-08-18-dev-prod-isolation-acceptance.md`

**Interfaces:**
- Consumes all prior runtime, scripts, health, and UI behavior.
- Produces an evidence record with commands, counts, temporary roots, profile responses, and hashes/counts showing production fixture data is unchanged.

- [ ] **Step 1: Write failing isolation integration tests**

Create two temporary `RuntimeConfig` objects and app instances. Assert every writable path differs, each health response matches its profile, development writes create only development files, and a pre-created production SQLite fixture has the same SHA-256 and row count after development startup/write/stop.

- [ ] **Step 2: Run and confirm RED on any remaining cross-environment behavior**

Run: `cd backend && .venv/bin/pytest tests/integration/test_runtime_isolation.py -q`

- [ ] **Step 3: Apply only the minimal fixes required by the isolation test**

Do not broaden this task into report or transcription refactoring. If a shared writable path is found, route it through `AppPaths` and add the exact path assertion to the test.

- [ ] **Step 4: Write/extend release whitelist tests before packaging changes**

Assert the archive includes required production runtime/config helpers but excludes `.runtime`, `.env*`, SQLite files, audio, logs, models, local dependency links, and test fixtures. Assert installer keeps the exact existing database path and invokes backup before changing `current`.

- [ ] **Step 5: Run release tests, then make the smallest whitelist/install changes if RED**

Run: `cd backend && .venv/bin/pytest tests/unit/test_release_package.py tests/unit/test_release_installer.py tests/unit/test_backup_data.py -q`

- [ ] **Step 6: Run the complete automated verification suite**

Run:

```bash
cd backend && .venv/bin/pytest -q
cd ../prototype && node --test tests/*.test.mjs
npm run build
cd .. && bash -n scripts/*.sh
```

Expected: zero failures and zero unexpected warnings. Record exact pass/skip counts.

- [ ] **Step 7: Run temporary dual-port lifecycle acceptance**

With temporary HOME/data roots, fake Keychain clients, `AUDIO_MEMORY_NO_OPEN=1`, and no provider calls, start production fixture on `8765` and development on `8766`. Query both health endpoints, exercise sequential start/stop, then simultaneous operation. Record PIDs, returned profiles, and non-overlapping temporary file lists. Do not point either fixture at the user's production database.

- [ ] **Step 8: Write the acceptance evidence**

Document baseline, red-green tests, full suite results, health responses, production fixture hash/row-count comparison, writable path matrix, release whitelist contents, and explicit confirmation that real Keychain/provider/audio/Release were untouched.

- [ ] **Step 9: Inspect the development UI manually**

Open only the temporary development instance. Confirm the badge is visible and restrained, then point the development frontend at a temporary fixture reporting production and confirm the blocking state prevents writes. Capture screenshots under ignored `prototype/output/`, not in the release archive.

- [ ] **Step 10: Commit Task 7**

```bash
git add backend/tests/integration/test_runtime_isolation.py backend/tests/unit/test_release_package.py backend/tests/unit/test_release_installer.py scripts/build-release.sh scripts/install-release.sh docs/qa/2026-08-18-dev-prod-isolation-acceptance.md
git commit -m "test: verify dev and production isolation"
```

### Final Candidate Review

- [ ] Re-read the design and map every completion criterion to test output or acceptance evidence.
- [ ] Confirm `git diff 0f7df7e..HEAD` contains no report-generation changes, real data, credentials, model files, dependency directories, or build output.
- [ ] Confirm `git status --short` is clean.
- [ ] Confirm `v0.1.0-beta.1` tag and Release were not modified.
- [ ] Stop before merge, push, tag, or beta.2 publication and request the user's explicit release decision.
