# ACE-style document learning

This implementation adapts procedural context with the existing service and model.
It does not change model weights, source documents, tool permissions or Skills.
The reference is [Agentic Context Engineering](https://arxiv.org/abs/2510.04618),
and the author implementation pinned at
[`82709de`](https://github.com/ace-agent/ace/tree/82709de050e1db6e6ef2f07bcb0393560b94992a).
The three roles and incremental playbook updates follow ACE. Scheduling a separate
reflection during document repair is our engineering extension; the pinned AppWorld
implementation chiefly curates at task completion or a cost trigger.

## Execution and learning boundaries

1. The planner executes ordinary registered tools. A source-bound, quoted negative
   document review, or repeated unproductive inspection recorded by the existing
   recovery ledger, can trigger a separate Reflector before the next planner turn.
   The latter can happen before the first candidate exists; a new observation alone
   is not an unproductive action or evidence of an incorrect recipe.
2. The Reflector sees that source's execution history, actual recipe, observed label
   transformations, candidate changes and cited source evidence. It can return no
   lesson. Cosmetic changes and repeated reads cannot trigger unlimited calls.
3. `repair_advice` carries the temporary reflection only to this task's matching source
   and method. It is explicitly unverified. `repair_adoption` records subsequent
   actions, changed output fields and the new review verdict; injecting advice alone
   does not prove the model acted on it or that the output improved.
4. At task completion, source-specific evidence is collected from the complete durable
   history. The Reflector and Curator produce bounded ADD/REVISE candidates. The Curator
   cannot rewrite the whole store or activate its own proposal. Separate sources do
   not lose early failures when another source occupies the last history window.
5. Only independently passing paired replay can activate retained advice. A new task
   receives matching active `playbook_advice`; temporary reflections stay in their
   original task. Retirement, expiration, method isolation and rollback remain intact.

Source evidence includes exact action IDs, immutable candidate paths, source byte
hashes and source-to-record mapping references. Views have bounded values and sample
counts; the full source/candidate/trace remains available. There is no site selector,
country-specific rule, expected label or answer in the generic learning scheduler.

The planner projection keeps one exact current review under `document_focus` and
replaces duplicate copies with explicit references. The original full context is
saved before projection. This leaves budget for the temporary reflection without
discarding the cited review or pretending a truncated view is the complete source.
If pinned context cannot fit the document planning performance target, the planner
can fall back to the existing actual prompt budget. The receipt records this fallback;
the model capacity, output reserve and admission ratio do not increase. A genuinely
over-budget pinned context still prevents a provider call.

## Configuration

`examples/documents-ace-gpt-6-luna.json` enables the new loop with the operator's
`http://localhost:15536/v1` service, native Responses and `gpt-6-luna`.
Credentials are read from `CDP_BROWSER_AGENT_API_KEY`.
The earlier `documents-gpt-6-luna.json` remains available as a learning-disabled control.

Extra work is bounded by `max_online_reflections` (3 per task),
`max_source_reflections` (2 per source), and `reflection_timeout_seconds` (60).
Existing `max_candidates`, `timeout_seconds`, replay sample count and processing
concurrency limits also apply. Cancelled, waiting and timed-out tasks do not start
terminal learning. Missing replay suites leave candidates inactive.

An inconclusive transient model review is separate from a cited semantic rejection.
`document_review_retry(job_id, expected_review_id)` can retry an unchanged candidate
within `documents.max_review_attempts` (2, maximum 3). Original attempts are preserved.
Ordinary `document_review` reads saved samples; it cannot silently rerun a model.
A stale review ID, an established semantic rejection, or an exhausted budget refuses
the retry. Unapproved candidates still cannot export.

## Replay that checks executed document output

Existing planner-decision and processing-output suites continue to work. A planner
suite can additionally set `evaluation: document_recipe`. Each independent case has
the public source URL, content and format in `document`, a truthful `initial_spec`
representing the repair decision, `task`, `observation`, and operator-only `expected`
keys with optional `text_by_key`. Expected values are never part of planner, Reflector,
Curator or reviewer inputs.

Baseline and candidate receive the same source, initial recipe, review and decision
budget (`max_steps`, 1–5). They differ only in retained playbook context. The real
planner runs allowed document tools; the host checks generated keys, optional complete
body values, recipe validity, review acceptance, conserved tree and unchanged source.
This tests executed document repair, not just whether a particular tool name appeared.
Fixture source bytes must differ from learning evidence and from other replay cases.
All candidate cases must pass and at least one baseline case must fail for admission.
When both variants already pass, the candidate correctly remains unadmitted.

## Validation protocol

Run tests before live validation. Freeze each live protocol/config/source snapshot in
a new directory before model calls. Controlled initial failures must be identified as
controlled; they do not prove an unseeded planner naturally made the same error.
Unseeded full tasks are evaluated separately with private source-specific checks only
after execution. Keep previous packages and run evidence immutable.

Report current-task repair, persistent candidate creation, paired replay admission,
fresh-task adoption and all four roles' measured cost separately. A successful local
test or one learned entry is not proof of general performance improvement or paper
reproduction.

See [the measured validation report](ACE_DOCUMENT_VALIDATION.md) and its
[machine-readable receipt index](validation/ace-documents-20261001.json) for the
current test, live repair, replay and cost evidence. The controlled and unseeded runs
are reported separately; earlier failed snapshots remain in the review package.
