# Beta 7 Hotfix 2 Cloud ASR Duration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `0.1.0-beta.7-hotfix.2` so valid Volcano AAC transcripts are not rejected when the provider duration is slightly longer than the local FFprobe duration.

**Architecture:** Keep local duration as the default trust boundary, but accept Volcano `audio_info.duration` when it is greater and the drift is bounded by `max(2_000 ms, 1% of local duration)`. Reject larger provider-duration drift before materializing utterances. Package the change as a new immutable Beta 7 hotfix without replacing Hotfix 1.

**Tech Stack:** Python 3.12, pytest, SQLite-backed Audio Memory backend, shell release gates.

**Spec:** User-reported `cloud_asr_failed` from the 2026-08-28 real six-file cloud transcription, reproduced by valid final utterances extending beyond FFprobe duration while remaining within Volcano `audio_info.duration`.

## Global Constraints

- Target version is exactly `0.1.0-beta.7-hotfix.2`.
- Completed transcription and reports must remain recoverable on retry.
- Provider duration drift over `max(2_000 ms, 1% of local duration)` must still fail closed.
- Do not publish, tag, push, merge, or replace an installed release without separate user approval.
- Do not repeat the paid real-audio smoke; use the already completed authorized six-file flow as real-flow evidence.

---

### Task 1: Bound provider-duration tolerance

**Files:**
- Modify: `backend/src/audio_memory/asr/normalizer.py`
- Test: `backend/tests/unit/asr/test_normalizer.py`

**Interfaces:**
- Consumes: `normalize_volcano_result(file_id: str, duration_ms: int, payload: dict[str, Any])`.
- Produces: normalized `CloudTranscriptSegment` values or `AsrResultError` for implausible duration drift.

- [x] **Step 1: Add failing tests for both observed AAC payloads and excessive drift**
- [x] **Step 2: Verify the observed final utterances fail against the old strict local-duration boundary**
- [x] **Step 3: Read and validate integer `audio_info.duration`, accepting only bounded positive drift**
- [x] **Step 4: Run normalizer and cloud-ASR recovery tests**

### Task 2: Version and release notes

**Files:**
- Modify: `VERSION`
- Modify: `backend/src/audio_memory/__init__.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: release version checks and package builder.
- Produces: consistent `0.1.0-beta.7-hotfix.2` source and package identity.

- [x] **Step 1: Update source version files**
- [x] **Step 2: Add a concise Hotfix 2 changelog entry**
- [ ] **Step 3: Run release-version and hotfix-governance regressions**

### Task 3: Verify and seal the candidate

**Files:**
- Verify: repository release allowlist and generated `dist/` artifacts

**Interfaces:**
- Consumes: source tree at `0.1.0-beta.7-hotfix.2`.
- Produces: one checksum-verified macOS arm64 archive ready for user acceptance.

- [ ] **Step 1: Run targeted ASR tests**
- [ ] **Step 2: Run the repository quality gate**
- [ ] **Step 3: Build the release archive once**
- [ ] **Step 4: Verify SHA-256 and archive allowlist**
- [ ] **Step 5: Inspect unpacked runtime import path without paid provider calls**
- [ ] **Step 6: Report the candidate and wait for publication approval**
