# Audio Memory v0.1.0-beta.3 Release Preparation Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce a reproducible, fully verified v0.1.0-beta.3 release candidate from the current integrated main while preserving the installed beta.2 environment and requiring separate approval before any GitHub publication.

**Architecture:** Work only in `codex/release-v0-1-beta3`, created from the verified main commit. Extend release governance so an already-integrated, clean main can be sealed into an immutable candidate only after the complete quality gate succeeds; keep the existing digest-bound build authorization. Build with verified bundled arm64 runtimes, then validate clean install, beta.2 upgrade, data retention, rollback, runtime isolation, and package contents in disposable homes.

**Tech Stack:** Python 3.11, pytest, Bash, Git worktrees, React/Vite, Playwright, SQLite/Alembic, GitHub CLI.

---

### Task 1: Freeze the release scope and isolated baseline

**Files:**
- Create: `docs/superpowers/plans/2026-08-20-v0-1-beta3-release-preparation.md`
- Create: `docs/qa/2026-08-20-v0-1-beta3-release-acceptance.md`

1. Confirm the release worktree is clean, on `codex/release-v0-1-beta3`, and based exactly on the current main.
2. Record the integrated feature scope and the fact that beta.2 production paths and services are excluded from all preparation tests.
3. Run the release-version, package, installer, governance, and runtime-isolation baseline tests.

### Task 2: Seal an already-integrated main through governance

**Files:**
- Modify: `backend/tests/unit/test_feature_governance.py`
- Modify: `scripts/feature_governance.py`
- Modify: `scripts/release-prepare.sh`
- Modify: `docs/superpowers/specs/2026-08-19-feature-track-and-release-governance-design.md`

1. Add tests proving a clean current main can be sealed only after its complete quality gate succeeds.
2. Add tests proving dirty/non-main worktrees, a moved HEAD, missing approval, and failed gates produce no valid integrated receipt.
3. Run the new tests and confirm they fail for the missing behavior.
4. Implement the minimal final-main candidate mode with a manifest digest and integrated receipt tied to the exact HEAD.
5. Run governance tests and confirm they pass.

### Task 3: Set the beta.3 product version and release notes

**Files:**
- Modify: `VERSION`
- Modify: `backend/src/audio_memory/__init__.py`
- Modify: `backend/tests/unit/test_release_version.py`
- Modify: `backend/tests/integration/test_health.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

1. Change tests to require `0.1.0-beta.3` and confirm the version test fails.
2. Update all runtime and documentation version sources consistently.
3. Add concise beta.3 notes covering analysis handoff recovery/logging, durable progress, upload/delete locking, interruption resume/cancel, provider ordering, and centered feedback.
4. Run version and health tests until green.

### Task 4: Build an immutable candidate package

**Files:**
- Verify: `scripts/build-release.sh`
- Verify: `scripts/verify-ffmpeg-runtime.py`
- Output: `dist/audio-memory-v0.1.0-beta.3-macos-arm64.tar.gz`
- Output: `dist/audio-memory-v0.1.0-beta.3-macos-arm64.tar.gz.sha256`

1. Verify the beta.2 archive checksum before extracting its already-published FFmpeg runtime into disposable preparation storage.
2. Verify FFmpeg/ffprobe and uv architecture and runtime behavior.
3. Run the complete quality gate on the exact release commit.
4. Seal the current main candidate with its digest-bound receipt.
5. Build the beta.3 archive without overwriting any beta.2 artifact.
6. Verify checksum, archive root, allowlist, executable modes, and absence of source-control, runtime data, logs, audio, databases, credentials, and governance records.

### Task 5: Validate install, upgrade, rollback, and product flow

**Files:**
- Modify: `docs/qa/2026-08-20-v0-1-beta3-release-acceptance.md`

1. Install beta.3 into a disposable clean HOME and verify version, health identity, packaged frontend, migrations, and doctor output.
2. Install beta.2 into another disposable HOME, seed representative history, upgrade using the beta.3 package, and verify backup creation plus byte/row-level history retention.
3. Exercise rollback to beta.2 in the disposable HOME and confirm preserved data remains readable.
4. Run a controlled fake-provider transcription-to-publication flow and verify persistent progress, final feed publication, interruption resume/cancel, upload/delete locks, and centered feedback behavior.
5. Confirm production port/path/Keychain namespaces do not overlap development and that the actual installed beta.2 environment was untouched.

### Task 6: Finalize the release branch for user approval

**Files:**
- Modify: `docs/qa/2026-08-20-v0-1-beta3-release-acceptance.md`

1. Re-run all backend, frontend, browser, release-package, installer, and runtime-isolation checks from the final commit.
2. Record exact commit, candidate digest, archive SHA-256, test counts, installation evidence, and known limitations.
3. Commit the release preparation on `codex/release-v0-1-beta3`.
4. Present the evidence and exact proposed mutations: merge to main, push main, create immutable tag, upload GitHub Release assets.
5. Do not perform those GitHub or tag mutations until the user explicitly confirms the final proposal.
