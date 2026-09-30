# ACE document learning: measured validation

The current implementation passes **229 tests** and dependency validation. The live
controlled fixture establishes separate reflection, an actual recipe/output repair,
terminal candidate creation and independent executed document replay. It establishes
**no persistent promotion, no fresh-task adoption, and no general performance gain**.

All calls used the operator's Cockpit service at `http://localhost:15536/v1`,
native `/responses` and `gpt-6-luna`. Credentials were supplied through the environment.
Each trial saved its config, protocol/task, raw model wire and exact source snapshot
before execution. Earlier failed runs remain preserved; they were not overwritten.

## Current controlled fixture

The operator supplied a numeric-prefix capture that loses part of a grouped original
label. A quoted independent model review rejected that candidate. The separate
Reflector returned temporary advice; the real Generator changed `key_transforms`,
produced distinct `4` and `4A` records, and passed a new review.

The terminal Reflector and Curator produced one inactive candidate. On two different
source byte strings, **baseline 2/2 and candidate 2/2 passed** actual document
generation and checks. The candidate therefore remained unadmitted. A fourth fresh
source was also processed correctly, with **zero active advice IDs**. This is baseline
capability, not observed cross-task experience adoption.

Three controlled trials preserve the same no-promotion outcome across the earlier
and current code snapshots. They are mechanism checks, not a powered comparison.

## Current unseeded three-source task

Fresh trial status: **incomplete** at step **51**; after one bounded resume with the soft-target fix, final status: **incomplete** at step **59**.
Exported record counts: `{"CN": 82, "JP": 464}`.
Online reflection attempts: **3**; subsequent planner decisions carrying temporary advice: **12**.
Actual changed-output receipts after advice: **2** (new review acceptance is recorded separately).

The task supplied known URLs and the desired output contract, without recipes or
site-specific tool instructions. Runtime and installed Skills remain generic. Private
country-specific assertions ran after model execution and never repaired candidates.
A correct individual export does not satisfy the requested three-source completion.

The resume carries the original saved sources, failed recipes, reflection budget and
exports. It receives no private oracle findings and starts no new repair experiment.
Its cost is reported separately; cumulative action/advice records are not counted as
new adoption. The raw checkpoint retains its earlier `stopped_reason`; the latest
termination is identified from the saved actual `done(outcome=incomplete)` action.
The latest controlled fixture snapshot precedes the final soft-target
fallback, which is verified by two added regression cases and this live resume.

- **China:** 82 exported records pass every source-specific post-run check. A temporary
  reflection preceded an `exclude_pattern` revision that changed article text and
  paragraphs, removed chapter titles from bodies and passed the new review.
- **United States:** no approved export. Changing body selection produced a real
  output delta, but the new review still found grouped or omitted section identifiers.
  A later `key_separator` edit changed no output; the source repair budget was exhausted.
- **Japan:** 464 article-node records were exported after model acceptance. The independent
  post-run check **fails grouped article range separation**, despite source/tree and
  body preservation passing. This is a review false acceptance for part of the contract,
  and the Japanese output is not a fully accepted delivery.

## Context failure and provider compatibility

The earlier unseeded trial stopped at a required-context budget failure immediately
after a U.S. reflection; that advice did not reach a subsequent Generator action.
The same current review was duplicated in several mandatory fields. The current
projection keeps one exact review plus recovery references, with the original full
context saved. On the exact frozen failed payload and calibrated token budget:

- Earlier projection: 26,223 estimated tokens; compaction failed.
- Current projection: 22,265 before compaction; 17,834 after; compaction committed.
- Review and temporary advice preserved exactly; **zero provider calls** in this check.

A second frozen failure occurred when pinned review/tool/advice context exceeded
the 18,000-token performance target, although its 22,356-token estimate fit the
existing 26,112-token model admission budget. The planner now records a soft-target
fallback and retains the original capacity/reserve/ratio. Offline verification
preserves review, advice and schemas exactly; the live resume passes the formerly
blocked planning point. Actual over-budget pinned context still refuses a model call.
The resume reaches a truthful incomplete final decision; it does not repair the
remaining American or Japanese output defects.

In the first controlled trial, 11 HTTP 400 responses preceded successful JSON-mode
fallbacks. The native Responses adapter now adds an explicit JSON input cue when
needed and includes its cost in prompt admission. Later controlled requests all
returned HTTP 200. Token usage of failed responses remains unknown, not zero.

## Measured call costs

Role order is Generator/planner, document reviewer, Reflector, Curator. Wire counts
include failed HTTP requests. Time is the sum of provider-call elapsed seconds;
it is neither task wall time nor monetary cost. Successful-request run metrics may
count fewer calls than the wire trace. Every role is included.

| Frozen run | Wire calls | Role calls G/review/R/C | Known input tokens | Missing usage calls | Wire seconds |
| --- | ---: | --- | ---: | ---: | ---: |
| controlled_before_json_cue | 31 | 7/20/2/2 | 126,350 | 11 | 159.891 |
| controlled_after_json_cue | 20 | 7/10/2/1 | 126,804 | 0 | 161.828 |
| unseeded_before_context_fix | 52 | 47/2/2/1 | 631,016 | 0 | 359.033 |
| controlled_current | 21 | 8/10/2/1 | 132,394 | 0 | 138.972 |
| unseeded_after_review_dedup | 62 | 50/5/5/2 | 763,640 | 0 | 490.936 |
| unseeded_resume_soft_target_fix | 12 | 8/0/2/2 | 142,326 | 0 | 90.640 |

These runs include code fixes, model variability and some concurrent execution.
They do not establish latency savings, token savings, or a causal quality gain.

## Evidence and limits

The [machine-readable index](validation/ace-documents-20261001.json) records every
run's actual checks, source hashes, role costs, review/output changes, replay outcomes
and receipt/file hashes. The portable review package includes full raw traces,
independent operator suites, saved sources/candidates and original frozen code.

The [method document](ACE_DOCUMENT_LEARNING.md) defines source/method isolation,
reflection budgets, transient review retry and replay admission. During-task
reflection is an engineering extension to the pinned ACE AppWorld scheduling.
Review and learning share a model family. A learned candidate, a passed mocked
admission test or advice injection alone is not evidence of empirical generalization.
