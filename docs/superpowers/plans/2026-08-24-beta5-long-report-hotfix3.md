# Beta 5 Long Report Hotfix 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover safely when a provider exhausts its output-token budget while generating the initial all-day report.

**Architecture:** Preserve the existing one-call path for normal reports. If and only if initial Markdown generation raises `model_output_truncated`, retry once with the same complete transcript and a strict complete-report length budget; validate and checkpoint only the successful complete response.

**Tech Stack:** Python 3.11, asyncio, pytest, Pydantic, existing provider abstraction.

**Spec:** `/Users/liujinxin/Downloads/AudioMemory-log-20260824-190922.txt`

## Global Constraints

- Base the release on immutable `v0.1.0-beta.5-hotfix.2`.
- Never publish or checkpoint truncated model output.
- Retry only `model_output_truncated`, at most once.
- The retry must still receive the complete transcript, profile, and user analysis goal.
- Keep logs free of transcript, prompt, model output, and credentials.

---

### Task 1: Initial-report truncation recovery

**Files:**
- Modify: `backend/src/audio_memory/prompts/composer.py`
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Test: `backend/tests/integration/test_single_report_runner.py`
- Test: `backend/tests/unit/prompts/test_composer.py`

**Interfaces:**
- Consumes: `ProviderAnalysisError.code`, `PromptComposer.compose_direct_report_markdown()`.
- Produces: `PromptComposer.compose_direct_report_markdown_compact_retry()` and one bounded retry in `SingleReportRunner`.

- [ ] Add an integration test whose first main-report call raises `model_output_truncated`, whose second returns valid Markdown, and which asserts exactly one compact retry is published.
- [ ] Run the focused test and confirm it fails because no recovery exists.
- [ ] Add a composer test asserting the retry has a distinct scene ID, complete transcript, and explicit report budget.
- [ ] Implement the compact retry request and runner recovery.
- [ ] Run focused tests and the full report-chain suite.
- [ ] Commit the code and tests.

### Task 2: Release verification and package

**Files:**
- Modify: `VERSION`
- Build: `dist/audio-memory-v0.1.0-beta.5-hotfix.3-macos-arm64.tar.gz`

**Interfaces:**
- Consumes: verified Task 1 commit.
- Produces: checksummed, unpacked-runtime-verified hotfix.3 archive.

- [ ] Set version to `0.1.0-beta.5-hotfix.3` and run version tests.
- [ ] Run targeted, backend, frontend, browser, and runtime-isolation gates required by the release scripts.
- [ ] Run real long-input acceptance for DeepSeek Flash, DeepSeek Pro, and Kimi when credentials are available; record any provider-side limitation honestly.
- [ ] Build the release archive and verify its SHA-256, allowlist, packaged prompts, and unpacked runtime.
- [ ] Commit release metadata, tag, push, and create a GitHub Pre-release under the user's existing publication authorization for hotfix updates.
- [ ] Verify the public assets and provide complete update commands.
