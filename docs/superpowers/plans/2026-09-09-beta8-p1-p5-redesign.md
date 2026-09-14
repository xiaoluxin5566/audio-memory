# Beta 8 P1-P5 Redesign Implementation Plan

**Goal:** Replace the overloaded Writing V1 contract with the approved five-stage user flow and run one untouched first result through P1-P5.

## Approved boundaries

- P1 registers complete Activities, Topics, exact evidence-anchor IDs, Attention Signals and structured Commitments. Activity is the only transcript boundary; Topics have no ranges.
- P2 reads every complete Activity transcript and only integrates Activities, applies absolute card-admission rules to every Topic, and makes explicit search decisions/tasks.
- P3 only executes a bounded public research task and returns source-grounded findings with limitations and a truthful failure status.
- P4 writes one card from its plan, complete source Activities, P1 navigation and P3 evidence. It cannot add cards, Topics, searches or repairs.
- P5 evaluates each unchanged P4 main card in one independent model call, using the 15/15/25/30/15 rubric. Every deduction states the concrete content that is missing or needs correction. It never edits cards.
- Program code validates identity, boundaries, IDs, source text and arithmetic. It does not assign semantic quality scores.
- No audit, revision, retry, publication or manual model-output correction is part of this run. A reproducible transport or contract bug may be fixed; the original failed artifact remains preserved.

## Implementation steps

1. Add strict V2 evidence contracts and failing tests for Topic-without-range, Activity-contained anchors, P2 absolute dispositions, complete Activity transcript input, focused P4 output and P5 hash-bound scoring.
2. Add versioned P1-P5 prompts and prompt fingerprints.
3. Add a separate P1-P5 pipeline/CLI beside the historical writing pipeline, with immutable first results, explicit budgets, one P5 call per main card and no repair path.
4. Run local contract/integration tests, copy only the new files to the protected Beta 8 worktree, and rerun the focused suite there.
5. Bind a new authorization to the new input and prompt hash, run the real P1-P5 user flow once, preserve the raw outcome, and open the development preview if cards and scores are available.

## Stop conditions

- Stop without resending when any model output is malformed or violates the approved semantic contract.
- Stop before dispatch if complete input exceeds the configured capacity; never truncate.
- Preserve P3 failure as a failed research packet and continue P4 only when the card can truthfully write without external evidence.
- A P5 failure preserves the V1 report and every earlier completed per-card score with `scoring_failed`; it never changes P4 output.
