# Feature Track and Release Governance Design

## 1. Goal

Audio Memory must enforce a repeatable release workflow without relying on a user repeatedly saying “create a branch” or “use the development environment.” The installed user version remains immutable at `v0.1.0-beta.2`; new work starts from `main`, runs on isolated feature tracks, merges to `main` only after approval and gates, and is released as `v0.1.0-beta.3` only after a separate release approval.

The unit of continuity is a feature track, not a Codex conversation. One feature may span several conversations, while a release-integration conversation may merge several completed feature tracks.

## 2. Non-goals

- Do not merge the current branch to `main` as part of implementing this governance.
- Do not publish, install, or replace the user-facing beta.2 release.
- Do not delete feature branches or worktrees automatically.
- Do not make GitHub availability a prerequisite for local development.
- Do not store transcripts, report content, credentials, or other user data in feature metadata.

## 3. Feature-track identity

Each independently releasable feature has one stable `feature_id`. It owns:

- branch `codex/<feature_id>`;
- one isolated worktree;
- one metadata file `.codex/features/<feature_id>.json`;
- zero or more Codex conversations;
- one final merge decision.

Starting an existing `feature_id` resumes its recorded branch and worktree. It must not create a second branch. Starting a new feature from a dirty or non-`main` source checkout is rejected unless the caller explicitly chooses a clean `main` checkout.

Feature metadata contains only repository workflow state:

```json
{
  "schema_version": 1,
  "feature_id": "report-progress",
  "branch": "codex/report-progress",
  "base_branch": "main",
  "target_version": "v0.1.0-beta.3",
  "status": "in_progress",
  "worktree": ".worktrees/report-progress",
  "head_commit": "<git commit>",
  "current_step": "real development acceptance",
  "required_checks": ["backend", "frontend", "browser", "runtime_isolation"],
  "passed_checks": [],
  "merge_approved": false
}
```

Valid states are `in_progress`, `ready_to_merge`, `merged`, `deferred`, and `released`. A failed integration gate moves the feature back to `in_progress`; it never silently remains ready.

Each feature uses its own file. There is no shared mutable feature index, which avoids merge conflicts when several branches are developed concurrently. Listing active features scans the per-feature files and verifies them against Git.

## 4. Runtime isolation

Feature development always uses:

- browser page `http://127.0.0.1:5173`;
- development backend `http://127.0.0.1:8766`;
- profile `development`;
- data root `<feature-worktree>/.runtime/dev`;
- development Keychain service `Audio Memory Dev`;
- shared read-only model assets when configured.

The browser-facing address is 5173. Port 8766 is an internal development API endpoint, not the page users are instructed to open.

The development frontend must reject a backend whose health identity is not `development`. Development startup must reject production data roots, aliases, symlinks, or other resolved paths that overlap the installed user version. The installed beta.2 runtime continues to use its own installation code, data, log, port 8765, and Keychain identity.

## 5. Commands and responsibilities

### `feature-start <feature_id>`

For a new feature, the command:

1. validates the identifier;
2. verifies a clean `main` source;
3. creates `codex/<feature_id>` and an isolated worktree;
4. writes the feature metadata atomically;
5. starts the development backend on 8766 and frontend on 5173;
6. verifies both runtime identities;
7. prints the 5173 browser address.

For an existing feature, it verifies and resumes the recorded branch and worktree. A metadata/Git mismatch is a hard error with recovery instructions; the command must not guess or overwrite state.

### `feature-status [feature_id]`

With an identifier, it reports the feature's verified branch, worktree, status, current step, commit, and check results. Without an identifier, it scans and lists every feature track. This is the recovery entry point for a new conversation.

### `feature-finish <feature_id>`

The command verifies that it is operating in the recorded feature worktree and that the worktree is clean. It runs the required backend, frontend, browser, and runtime-isolation gates. Only an all-green result updates the metadata atomically to `ready_to_merge` and records the tested commit. Any later commit invalidates the recorded result and returns the feature to `in_progress`.

### `release-prepare <version>`

This command runs from a clean `main`. It scans `ready_to_merge` tracks and produces a release candidate manifest but performs no merge. The manifest records the target version, selected feature IDs, exact tested commits, proposed merge order, and current `main` commit.

### `release-integrate <manifest>`

This command requires explicit user approval of the manifest. It integrates one feature at a time. Before each merge it verifies that the feature is still `ready_to_merge` at the recorded commit. After each merge it runs bounded integration checks; after the final merge it runs the complete suite.

If a conflict or test failure occurs, integration stops immediately. No later feature is merged and no release is produced.

### `release-build <version>`

This command requires a second explicit approval after integration succeeds. It runs only from a clean `main`, validates the version and candidate manifest, runs final release checks, creates the immutable version tag, and delegates packaging to the existing release builder. It never deletes feature branches or worktrees.

## 6. Integration failure policy

`main` is an integration branch and must remain runnable. Feature corrections are not authored directly on `main`.

When feature 3 fails before merge:

1. stop integration;
2. mark feature 3 `in_progress`;
3. switch to its recorded branch/worktree;
4. fix and retest there, in the same conversation or another conversation;
5. run `feature-finish` again;
6. regenerate or refresh the release candidate before continuing.

Small conflict adaptations may be coordinated from the integration conversation, but the commit still belongs to the feature branch. Larger requirement changes should move to a dedicated continuation conversation.

When a problem is discovered only after merge but before release, create a dedicated `codex/<feature_id>-integration-fix` track from the current `main`, test it independently, and integrate it through the same gate. Do not rewrite unrelated merged history.

## 7. Conversation protocol

Repository state, not conversation memory, is authoritative.

Typical user requests are:

- `开发功能：<feature_id>` — create a new feature track.
- `继续功能：<feature_id>` — verify and resume an existing track.
- `列出进行中的功能` — scan feature metadata and Git state.
- `准备 beta.3 集成，先生成候选清单` — report candidates without merging.
- `确认按清单逐个合并` — authorize integration only.
- `确认发布 beta.3` — authorize tagging and packaging only after integration acceptance.

Changing conversation does not create a branch. A new conversation must run `feature-status` before changing a continuing feature. A request for a different independently releasable feature creates a different feature track.

## 8. Repository and GitHub enforcement

The root `AGENTS.md` defines the mandatory workflow for coding agents:

- no feature or defect implementation directly on `main`;
- no development writes outside the recorded development runtime;
- no merge, release, branch deletion, or worktree deletion without explicit user approval;
- feature continuation must restore recorded state before editing;
- completion claims require the recorded gates.

Local pre-commit/pre-push hooks provide fast feedback but are not the only protection because hooks can be bypassed. CI is authoritative for merge eligibility. GitHub branch protection should require pull requests and the same named checks before `main` accepts a hosted merge. Local integration remains supported, but it must use the same gate runner and record equivalent evidence.

## 9. Safety and atomicity

- Metadata writes use a temporary sibling, file synchronization, and atomic replacement.
- Paths are resolved and verified against the repository and development-root boundaries before writes.
- Branch, worktree, metadata, and current HEAD must agree before any mutating command.
- Commands default to inspection and stop on ambiguity.
- Destructive cleanup is a separate future operation and always requires a displayed target list plus explicit approval.
- Release manifests contain repository identifiers and commits only; no runtime or user content.

## 10. Testing and acceptance

Automated tests must prove:

- a new feature creates the correct branch, worktree, and metadata;
- an existing feature resumes without creating another branch;
- dirty `main`, invalid identifiers, branch mismatches, and worktree mismatches fail safely;
- development startup exposes 5173 and connects only to development 8766;
- production-root aliases and runtime overlap are rejected;
- a new conversation can reconstruct feature state using only repository artifacts;
- a new commit invalidates `ready_to_merge` evidence;
- integration processes features sequentially and stops at the first conflict or failed check;
- corrections are committed to the feature track, not directly to `main`;
- release building is impossible before integration acceptance and a second approval;
- beta.2 installed runtime and data remain unchanged throughout all fixtures;
- no command deletes a branch or worktree.

Manual acceptance must demonstrate two concurrently developed features, continuation of one feature from a fresh conversation context, sequential integration into a temporary fixture repository, a deliberate failure on the second feature, successful recovery on its feature branch, and final beta.3 candidate generation without publishing.

## 11. Rollout

The governance is introduced on `codex/beta3-stability` and tested there. It does not retroactively rewrite existing branch histories. Existing valuable tracks can be enrolled by an explicit read-only audit followed by metadata creation after the user confirms the mapping. After the governance itself passes review, it may be merged to `main` through the current manual process. Only subsequent feature tracks are required to use the new automated workflow.
