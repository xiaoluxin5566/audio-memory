# Report Audit and Targeted Revision Pipeline Design

**Date:** 2026-08-17

**Status:** Approved in chat; written specification pending user review

## Goal

Replace the current direct-report flow of “generate Markdown, review and rewrite in one call, then ask a model to add UI annotations” with a content-quality flow that:

1. generates a complete V1 Markdown report from the full reliable transcript;
2. audits V1 against the full reliable transcript and produces a scored, evidence-backed issue packet;
3. revises only affected sections using the issue packet and bounded evidence, without rereading the full transcript;
4. audits the V2 revision result using the V1-to-V2 diff and bounded evidence, without rereading the full transcript;
5. publishes the best available report even when audit or revision stages fail;
6. compares the new flow with the existing three-pass baseline on the same historical transcript, including latency and content quality.

## Non-goals

- Do not restore the legacy autonomous-analysis or six-scene card pipelines to production.
- Do not introduce richer UI semantics, card classification, visual styling instructions, or a model-based annotation call.
- Do not require every stage to succeed before a report can be published.
- Do not add repeated revision loops beyond one targeted revision.
- Do not claim that a bounded final audit performs a second full-transcript coverage audit.

## Pipeline

### Stage 1: Generate V1

The generation call receives:

- the complete reliable transcript rendered as Markdown;
- the frozen profile snapshot;
- the frozen user analysis goal;
- system safety and factual-boundary rules;
- the report generation prompt, including the Markdown document contract.

It returns a complete Markdown report, stored as `direct_report_v1_markdown`.

The generation prompt is responsible for content quality and lightweight document structure. It must not produce UI component types, layout parameters, or a UI JSON schema.

### Stage 2: Full V1 Audit

The first audit call uses `audit_mode=full_v1_audit` and receives:

- the complete reliable transcript;
- the complete V1 report with stable section IDs;
- the frozen profile snapshot and user analysis goal;
- deterministic Markdown gate failures, if any;
- the shared report-audit prompt and audit schema.

This stage must perform full transcript coverage. It has two separate responsibilities:

1. validate content already present in V1;
2. find important transcript content omitted from V1.

It returns:

- a 100-point total score and five component scores;
- `passed` and coverage status;
- evidence-backed `critical`, `major`, and `minor` issues;
- a self-contained revision packet for every material issue;
- coverage summaries that state whether all supplied files and transcript ranges were reviewed.

If the call fails, times out, or produces invalid output, the system publishes V1 with no score and status `completed_unaudited`.

If the audit succeeds and reports no material issue requiring revision, the system publishes V1 with its audit score and status `completed_v1_audited`.

### Stage 3: Targeted Revision to V2

The revision call receives no full transcript. It receives:

- the V1 report title and section outline;
- only the complete sections allowed to change;
- necessary adjacent-section context;
- the audit issue packets;
- evidence excerpts and context excerpts captured by the V1 audit;
- the exact allowed evidence segment IDs;
- the exact allowed and forbidden section IDs;
- the targeted-revision prompt and revision schema.

It returns complete replacement Markdown for affected sections, issue IDs resolved by each replacement, unresolved issue IDs, and a revision summary.

Server-side validation must reject:

- unknown or duplicate section IDs;
- title mismatches;
- evidence IDs outside the allowed issue packet;
- material issues that disappear without being resolved or declared unresolved;
- abnormal section compression unless the audit explicitly authorizes repetition removal;
- changes to sections outside the allowed set.

If revision fails, the system publishes V1 with the V1 audit score and status `completed_v1_revision_failed`.

If revision succeeds, the merged candidate is stored as `direct_report_v2_markdown`.

### Stage 4: Bounded V2 Final Audit

The final audit reuses the shared audit prompt with `audit_mode=revision_final_audit`. It receives no full transcript. It receives:

- the complete V2 report;
- the V1-to-V2 section diff;
- all V1 audit issues;
- the revision result and claimed resolved issue IDs;
- the before and after versions of modified sections;
- the bounded evidence and context excerpts for those issues.

It verifies:

- whether each material V1 issue was actually resolved;
- whether revisions remain faithful to supplied evidence;
- whether revisions introduced new local factual, identity, quotation, todo, or causal errors;
- whether correct and valuable content was lost;
- whether V2 is coherent and structurally valid.

It does not claim to rediscover arbitrary omissions outside the V1 full audit.

If final audit succeeds technically, V2 is published whether `passed` is true or false. Its final audit score is displayed and stored.

If final audit fails technically, V2 is still published. The last valid score is the V1 audit score, but it must be marked with scope `v1_pre_revision`; it must not be represented as a V2 score.

## Early Exit

The system uses at most four model calls:

- V1 audit has no material revision issue: two calls, publish V1;
- V1 audit has material issues and later stages succeed: four calls, publish V2;
- audit or revision technical failure: publish the latest valid report immediately according to the degradation rules.

The system must not force revision merely to consume all four calls.

## Prompt Organization

The production path uses three business prompt files plus shared system rules:

1. `direct-report-generation.md`: analysis rules and Markdown report contract;
2. `direct-report-audit.md`: shared scoring and issue-discovery rules, parameterized by `audit_mode`;
3. `direct-report-revision.md`: bounded, minimal section-replacement rules;
4. `direct-report-system.md`: shared safety, factual-source, identity, and diagnosis boundaries.

The composer exposes separate interfaces even though the two audit calls share one prompt file:

- `compose_full_report_audit(...)`;
- `compose_targeted_report_revision(...)`;
- `compose_revision_final_audit(...)`.

This keeps runtime inputs explicit and permits later prompt-file separation without changing the runner state machine.

## Audit Score V1

The initial rubric is 100 points:

| Dimension | Maximum | Meaning |
| --- | ---: | --- |
| Factual and evidence accuracy | 30 | Facts, identities, entities, quotations, todos, event ownership, and causal claims match evidence. |
| Important-content coverage | 25 | Important events and findings are covered without major omission or incorrect merging. |
| Analysis depth | 20 | The report explains importance, causes, effects, relationships, difficult points, responses, and open loops. |
| Advice and actionability | 15 | Advice is relevant, concrete, executable, and includes conditions, risks, success signals, or wording when useful. |
| Expression and structure | 10 | The report is natural, coherent, non-repetitive, readable, and not mechanically templated. |

Scoring constraints:

- total score equals the five component scores;
- any unresolved `critical` issue caps the total at 59;
- any unresolved `major` factual issue caps the total at 69;
- `passed=true` requires at least 75 points and no unresolved `critical` or `major` issue;
- a failed quality result does not block publication;
- every deduction must have a concrete reason.

The rubric is explicitly versioned as V1 and will be calibrated using historical comparison results.

## Publication Status and Score Scope

The persisted report metadata must distinguish pipeline progress from report publication:

| Internal status | User-facing status | Published version | Score |
| --- | --- | --- | --- |
| `completed` | `已完成，{score}分` | V1 or V2 | Latest completed audit score |
| `completed_unaudited` | `已完成（未审计）` | V1 | None |
| `completed_v1_revision_failed` | `已完成（V1），{score}分` | V1 | V1 full-audit score |
| `completed_v2_final_audit_degraded` | `已完成（V2），V1审计{score}分` | V2 | V1 score with scope `v1_pre_revision` |

When V2 final audit completes but returns `passed=false`, the internal status remains `completed`, V2 is published, and the V2 score is shown. Quality pass/fail remains separately queryable.

Required metadata:

- `report_version`: `v1` or `v2`;
- `audit_status`;
- `quality_score`: integer or null;
- `quality_score_scope`: `v1_full_audit`, `v1_pre_revision`, `v2_final_audit`, or null;
- `quality_passed`: boolean or null;
- component scores;
- unresolved issue IDs and severity counts;
- technical degradation reason, if any.

## UI Semantics

Remove the model-based `direct-report-annotations` call from the production path.

The report remains Markdown. Existing deterministic parsing may infer page titles, section headings, subheadings, quotations, lists, tables, and paragraphs. No model call is needed because the current product does not require finer semantic UI labels.

## Checkpointing and Resume

Persist each completed artifact independently:

- V1 Markdown;
- V1 full audit;
- V2 section revisions;
- V2 Markdown;
- V2 final audit;
- publication metadata and degradation reasons.

Resume must reuse valid completed stages. Frozen model, credential generation, prompt hashes, transcript fingerprint, profile snapshot, and user goal must remain compatible. A resumed run must not repeat a successful model call.

## Failure Policy

| Failure | Publication behavior |
| --- | --- |
| V1 generation fails | No usable report exists; analysis fails. |
| V1 full audit fails technically | Publish V1 as unaudited. |
| V1 audit passes with no material issues | Publish V1 with score. |
| Targeted revision fails technically or semantically | Publish V1 with V1 score. |
| Deterministic V2 merge or Markdown validation fails | Publish V1 with V1 score. |
| V2 final audit fails technically | Publish V2 with degraded final-audit status and scoped V1 score. |
| V2 final audit completes with quality failure | Publish V2 with final score and `quality_passed=false`. |

Errors must be recorded without exposing provider raw responses or secrets.

## Historical Evaluation

Use the same historical transcript and profile snapshot used by the existing three-pass DeepSeek run when possible:

- 5,199 transcript segments;
- 326,846 transcript characters;
- DeepSeek model configuration matching the baseline when still available.

Run the old and new flows from frozen inputs. Do not compare runs using different transcripts, profiles, user goals, or model settings without labeling the difference.

Compare:

- model call count;
- per-call and total provider latency;
- input and output token usage;
- report character count;
- audit total and component scores;
- material issue counts;
- factual corrections and important omissions;
- analysis depth, advice quality, repetition, and structural quality;
- whether any revision introduced content loss;
- final publication status and degradation path.

Because the old baseline lacks the new scoring rubric, run the same new audit rubric against both final reports. The comparison must distinguish model-generated scores from deterministic metrics and include a short human-readable qualitative analysis.

## Testing

Implementation follows test-driven development. Required coverage includes:

- composer contracts for all three Prompt types and both audit modes;
- full transcript included only in generation and V1 audit;
- targeted revision and final audit reject or omit full transcript input;
- no-material-issue early exit after V1 audit;
- V1 publication when full audit fails;
- V1 publication when revision fails;
- V2 publication when final audit fails technically;
- V2 publication when final audit returns `passed=false`;
- score and score-scope correctness for every status;
- checkpoint resume without duplicate model calls;
- deterministic Markdown block inference without annotation calls;
- compatibility of publication and content APIs;
- historical old-versus-new comparison artifact generation.

## Acceptance Criteria

1. Production no longer calls the model for report UI annotations.
2. Only generation and V1 audit receive the full transcript.
3. V1 audit produces a scored, evidence-backed issue packet adequate for transcript-free targeted revision.
4. V2 final audit uses only revision context and bounded evidence.
5. Audit and revision failures publish the correct latest report with truthful status and score scope.
6. A completed low-quality final audit does not block V2 publication.
7. Existing unrelated working-tree changes remain intact.
8. Automated tests pass.
9. A real historical new-flow run is completed when provider credentials and connectivity are available.
10. A comparison document reports old versus new latency and content quality, and labels any unavailable or incomparable measurements.
