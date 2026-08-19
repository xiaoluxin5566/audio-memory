# Self-contained Audio Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship and verify `ffmpeg` and `ffprobe` inside the macOS arm64 release so valid MP3/AAC uploads work without Homebrew.

**Architecture:** A release-time builder prepares a pinned, manifest-backed FFmpeg runtime. Packaging, installation, startup, backend subprocesses, diagnostics, and upload error presentation all consume that single bundled runtime contract.

**Tech Stack:** Bash, Python 3.12, FFmpeg, pytest, macOS launchd

**Spec:** `docs/superpowers/specs/2026-08-19-self-contained-audio-runtime-design.md`

## Global Constraints

- First release supports macOS Apple Silicon only.
- End-user installation must not require or install Homebrew.
- FFmpeg must be built with `--disable-gpl` and `--disable-nonfree` and include build provenance and checksums.
- A failed install must preserve the existing `current` release and all user data.
- Existing dirty worktrees and Keychain data are out of scope.

---

### Task 1: Runtime manifest verification

**Files:**
- Create: `scripts/verify-ffmpeg-runtime.py`
- Create: `backend/tests/unit/test_ffmpeg_runtime_bundle.py`

**Interfaces:**
- Consumes: runtime root containing `bin/ffmpeg`, `bin/ffprobe`, and `manifest.json`.
- Produces: `verify-ffmpeg-runtime.py RUNTIME_ROOT`, exit 0 only for matching executable arm64 binaries.

- [ ] Write tests using executable fixtures and literal SHA-256 values for valid, missing, and tampered runtimes.
- [ ] Run `pytest -q tests/unit/test_ffmpeg_runtime_bundle.py` and verify failure because the verifier is absent.
- [ ] Implement JSON/schema, executable, SHA-256, subprocess version, and optional arm64 `file` validation.
- [ ] Re-run the test and verify pass.

### Task 2: Release archive includes the runtime

**Files:**
- Modify: `scripts/build-release.sh`
- Modify: `backend/tests/unit/test_release_package.py`

**Interfaces:**
- Consumes: `AUDIO_MEMORY_FFMPEG_RUNTIME` or repository `vendor/ffmpeg-darwin-arm64`.
- Produces: archive paths `runtime/ffmpeg/bin/{ffmpeg,ffprobe}`, manifest, license, and provenance.

- [ ] Extend the package test with a fake verified runtime and assertions for all required archive paths.
- [ ] Run the targeted test and verify failure because the builder ignores the runtime.
- [ ] Make the builder verify and copy only the runtime whitelist before creating the archive.
- [ ] Re-run the package test and verify pass.

### Task 3: Installer validates before switching versions

**Files:**
- Modify: `scripts/install-release.sh`
- Modify: `backend/tests/unit/test_release_installer.py`

**Interfaces:**
- Consumes: packaged runtime contract from Task 2.
- Produces: installed executable runtime, or a nonzero result with prior `current` unchanged.

- [ ] Add fixture runtime files plus tests for valid install and tampered-runtime rollback.
- [ ] Run the targeted test and verify failure on the missing installer gate.
- [ ] Invoke the verifier on the temporary version before atomic rename/link switching.
- [ ] Re-run installer tests and verify pass.

### Task 4: Runtime tool resolution

**Files:**
- Create: `backend/src/audio_memory/runtime_tools.py`
- Create: `backend/tests/unit/test_runtime_tools.py`
- Modify: `scripts/start.sh`
- Modify: `backend/tests/unit/test_audio_memory_cli.py`

**Interfaces:**
- Produces: `resolve_runtime_tool(name: Literal["ffmpeg", "ffprobe"]) -> str`.
- Resolution order: explicit environment path, bundled path, then system path only outside release mode.

- [ ] Write tests for explicit, bundled, development fallback, and release-mode missing behavior.
- [ ] Run tests and verify failure because the resolver does not exist.
- [ ] Implement the resolver and export bundled runtime paths in `start.sh`.
- [ ] Re-run tests and verify pass.

### Task 5: Migrate audio subprocesses and errors

**Files:**
- Modify: `backend/src/audio_memory/uploads/probe.py`
- Modify: `backend/src/audio_memory/diarization/engine.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Modify: `backend/tests/integration/test_upload_jobs.py`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/tests/product-state.test.mjs`

**Interfaces:**
- Consumes: `resolve_runtime_tool` from Task 4.
- Produces: stable `audio_runtime_unavailable` for missing tools; `unsupported_format` only for decoded content/extension mismatch.

- [ ] Add backend and client regression tests reproducing missing-tool and generic-service failures.
- [ ] Run targeted backend/client tests and verify expected failures.
- [ ] Replace bare tool names, map dependency failure explicitly, and restrict invalid UI state to `unsupported_format`.
- [ ] Re-run targeted tests and verify pass.

### Task 6: Diagnostics and release acceptance

**Files:**
- Modify: `scripts/doctor.sh`
- Modify: `README.md`
- Modify: `PRIVACY.md`
- Create: `THIRD_PARTY_NOTICES.md`
- Modify: `docs/working/2026-08-18-v0-1-beta-1-verification.md`

**Interfaces:**
- Consumes: installed bundled runtime and manifest.
- Produces: user-visible diagnostic result and redistribution notice.

- [ ] Add a doctor contract test that runs with a minimal system `PATH` and verifies bundled tools.
- [ ] Run it and verify failure while doctor checks the system command.
- [ ] Point diagnostics at the manifest-backed runtime and document source/license/build requirements.
- [ ] Run targeted tests, all backend unit tests, frontend tests, and a release archive smoke test.
