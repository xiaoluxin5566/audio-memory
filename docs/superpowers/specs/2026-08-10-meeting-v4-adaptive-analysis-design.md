# Meeting V4 Adaptive Analysis Design

**Date:** 2026-08-10  
**Status:** User-approved direction  
**Scope:** Meeting Prompt, Meeting output Schema, meeting card detail rendering, prompt migration, and one real-history reanalysis. No new model stage, planner, deduper, retranscription, web verification, or changes to the other five scenes.

## Goal

Let the existing single Meeting model call decide semantic card boundaries across all routed meeting dossiers and produce rich, evidence-backed analysis with representative quotes, both sides' arguments, adaptive sections, and targeted recommendations. Replace the fixed meeting-detail presentation without adding another engineering decision layer.

## Product decisions

1. A card is an independent analysis theme, not an Event, dossier, audio interval, or conversation boundary.
2. One conversation may support several scene cards; separate conversations may support one shared theme card.
3. The Meeting model reads all meeting dossiers in one request and owns merge/split decisions.
4. The service validates structure, Event authorization, dossier evidence, identity, and playback only. It does not semantically merge, split, rank, or rewrite cards.
5. The old background/topics/conclusions/decisions/questions/actions template is removed from the Meeting Schema and UI.
6. The model receives explicit anti-duplication and richness rules through Meeting Prompt V4.
7. Facts, model analysis, uncertainty, and recommendations remain distinguishable.

## Adaptive Meeting Schema

The outer scene contract remains strict and versioned. Each card contains:

- `event_ids`: one or more authorized Event anchors; multiple Events may support one semantic card.
- `card`: title and summary.
- `confidence`.
- `detail.analysis_angle`: the card's independent question/value.
- `detail.context_summary`: necessary context in narrative form.
- `detail.participants`: evidence-backed roles.
- `detail.key_facts`: evidence-backed facts with optional interpretation.
- `detail.quote_analyses`: verbatim short quote, context, surface meaning, deeper analysis, interaction effect, and evidence.
- `detail.arguments`: speaker position, reasoning, supporting facts, assumptions, responses, counterpoints, assessment, and evidence.
- `detail.recommendations`: observed issue, evidence basis, importance, recommendation, actions, optional suggested language/expected result/caveat, and evidence.
- `detail.sections`: model-chosen sections with semantic type, natural title, narrative, key points, and evidence.
- `detail.uncertainties`: unresolved or conflicting information with evidence.

Every evidence-bearing object names its authorized `event_ids` and `evidence_segment_ids`. This supports a card that merges multiple Event anchors while keeping every atomic claim inside at least one routed dossier.

The Schema remains strict about field types and unknown fields, but the model controls section count, names, order, types, narrative length, and which optional analytical fields are populated. `quote_analyses`, `arguments`, `recommendations`, and `sections` are independent first-class fields rather than content hidden under fixed meeting headings.

## Evidence and identity

- Every quote is verbatim from reliable transcript evidence.
- Each atomic fact, quote analysis, argument, recommendation, section, and uncertainty must fit a single dossier whose authorized Events and allowed segments cover the reference.
- A multi-Event item may cite several Events only when one routed dossier authorizes the full atomic evidence statement; otherwise it must be split.
- The validator does not judge whether analysis is insightful; that remains the model's responsibility.
- Unknown identity uses objective roles and cannot generate user/shared global todos or personal claims.
- Existing card-scoped audio playback recursively discovers the new evidence fields and remains unchanged.

## Prompt V4 behavior

The packaged Meeting Prompt is the exact user-approved V4 draft from this conversation. Its primary requirements are:

- read all meeting dossiers before deciding cards;
- merge the same interview or shared analytical theme even across Events/dossiers;
- split only for independent questions, evidence, conclusions, and user value;
- compare planned cards and merge semantic duplicates before output;
- reconstruct interaction flow and both sides' positions;
- include representative verbatim quote analysis;
- separate facts, interpretations, and uncertainty;
- produce specific evidence-grounded recommendations, never generic advice;
- choose adaptive sections instead of filling a fixed template;
- self-check boundary quality, richness, duplication, and usefulness.

## UI direction

Keep the existing restrained local-product visual language. Meeting detail becomes an editorial analysis document:

1. analysis angle and context lead;
2. participants appear as compact role chips;
3. key facts use concise evidence cards;
4. quote analyses use visually distinct quotation panels followed by interpretation;
5. arguments use side-by-side or stacked position panels depending on width;
6. model-chosen sections render in declared order;
7. recommendations use actionable cards with steps and suggested language;
8. uncertainties remain visibly separate;
9. evidence playback remains at the end of the card.

No new page, navigation item, card type, or visual redesign is introduced.

## Prompt migration

Meeting packaged default advances from V3 to V4. PromptStore archives an untouched packaged V3 and upgrades it once. A user-edited prompt is never silently overwritten. For this approved local run, the active Meeting prompt is explicitly saved as V4 before preview/reanalysis.

## Real-data acceptance

Reuse the existing 3,442 reliable segments without Whisper or diarization.

- The two current interview cards become one card covering the complete interview theme.
- Friend-chat output may be one or more cards only when their analysis angles and value are clearly distinct; overlapping themes must not produce redundant cards.
- Meeting detail visibly contains short quote analysis, both sides' arguments, key facts, adaptive analysis, and targeted advice when supported.
- Advice names its evidence, reason, concrete action, and uncertainty.
- No dossier-outside evidence, invented quote, invented identity, decision, owner, or deadline.
- Evidence playback works for at least one new quote/analysis item.
- Other scenes, atomic publication, history, and reanalysis behavior do not regress.

## Out of scope

- Additional model calls or a card-planning stage
- Deterministic semantic deduplication or similarity thresholds
- Richness post-processing by the service
- Changes to parenting, content, growth, inspiration, or todo output schemas
- Web verification and Compact transcription

