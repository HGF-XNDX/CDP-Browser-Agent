# Versioned procedural learning (0.10)

This release adds an ACE-inspired learning layer to the existing harness. It adapts
procedural context, not model weights, source code, skills or permissions. The browser
planner and processing workers share a versioned store, while keeping separate scopes.

## Enable and run

`examples/playbook-30000.json` enables the full learning pipeline against the existing
OpenAI-compatible service on port 30000. The sample is scoped to `release-collection`;
its `playbook-heading` profile and held-out suite demonstrate learning a section preference.
General package defaults leave the additional model calls disabled. Set `learning.enabled`
to true in an operator configuration to activate automatic learning and recall.
The existing `examples/harness-30000.json` enables general candidate learning with no
replay suites, so it will accumulate candidates without automatically adopting them.

```powershell
python -m cdp_browser_agent.browser --config examples/playbook-30000.json --worker-start playbook-heading --processing-input examples/processing-inputs/heading.json
python -m cdp_browser_agent.browser --config examples/playbook-30000.json --worker-continue WORKER_ID --worker-turn 1 --worker-feedback "提取 Releases 栏目下第一条发布标题，保留原文"
python -m cdp_browser_agent.browser --config examples/playbook-30000.json --playbook-list
```

Use a distinct operator `task_type` for workflows with different preferences. A lesson
from one customer's column preference must not become a general extraction rule.
For an ordinary browser configuration use `task_type: general` and register appropriate
planner replay suites. Unmatched lessons remain candidates; their own success claims
cannot activate them. CLI, MCP and Python calls use the same store.

## Learning lifecycle

1. The harness records bounded, source-linked task evidence. Browser runs trigger learning
   on failures/recoveries or unsuccessful terminal outcomes; `reflect_success: true` also
   includes successful runs. Waiting, cancelled and timed-out runs do not start reflection.
   Workers learn after a feedback continuation. Initial successful worker turns are not
   automatically interpreted as a reusable preference.
2. The Reflector proposes concrete lessons, each with `trigger`, `guidance`, `avoid` and
   actual `evidence_ids`. Unknown references or extra fields reject the proposal.
3. The Curator emits bounded `ADD` or `REVISE` deltas. A revision must reference the current
   version it saw. SQLite applies updates; model output cannot activate or retire entries.
4. Operator replay compares the current baseline with the candidate on distinct inputs.
   All candidate cases must pass and at least one baseline case must fail. Expected
   answers stay outside generator, Reflector and Curator inputs. The original learning
   inputs, including deduplicated origins, are excluded from replay.
5. Passing candidates become active. Failed/newer candidates leave the previous active
   version intact. A fresh replay that finds regression in an active version demotes it.
   Retirement wins over in-flight adoption; validated older versions can be restored.

Each entry is limited by exact target, host, task type, profile and method fingerprint.
The planner fingerprint includes the resolved model/configuration, package version,
system instructions, tool definitions and active skills. Processing uses the existing
frozen method/model/skill fingerprint. Host scopes do not imply permission to access a site.
Mixed-site processing datasets have a distinct scope; single-site experience is not injected.

`learning.max_entries` (default 4) and `max_context_chars` (6000) bound recalled context.
Selection uses scope filtering and simple lexical relevance, with recency as a tie-breaker.
This is not embedding retrieval or semantic contradiction detection. Exact text duplicates
share an identity and retain all source origins. Each version expires after `ttl_days`
(default 30); a fresh observation can propose a new candidate for expired advice.
Explicitly retired advice does not revive automatically.

## Replay contracts and limits

Suites are JSON files in operator-configured `learning.replay_paths`. Each has `name`,
`target`, `task_type`, `host`, optional processing `profile`, and 2–10 distinct `cases`.
The sample processing suite is `examples/playbook-replays/release-headings.json`.

A planner case supplies `task`, `observation`, optional `last_result`/`browser_started`,
and a nonempty `expected` action object. The real planner produces a decision; normal
action policy validates it, then the verifier checks the expected object as a recursive
subset. Replay does not execute its proposed browser or tool actions. This checks
decisions, not whether an entire interactive workflow succeeds.
Represent the actual decision point: for an external tool already discovered in the
trace, supply its `tool_describe` result as `last_result`. A fresh planner may correctly
discover a tool before calling it; that discovery is a different decision to evaluate.

A processing case supplies `record` and exact `expected` output data. The real processing
engine runs, writes files and checks the registered schema/source/contract plus the
held-out expected object. Full results and paired reports are preserved.

`auto_replay` defaults to true when learning is enabled, using the first matching operator
suite. No suite means no activation. Reflection and curation use at most two calls per
event; at most `max_candidates` (1–2) candidates are produced. Each paired suite costs
two generator runs per case, plus any configured processing repairs. A shared
`timeout_seconds` budget bounds the learning operation. Extra model work consumes the
same configured service; a large context window does not remove this cost.
Reflection and replay share the processing concurrency admission limit. A busy service
defers learning before model calls. Reopening a completed feedback worker can finish
deferred/interrupted learning without rerunning the committed processing turn; completed
learning events are idempotent.

Small replay suites are admission checks, not statistical proof of general improvement.
Schemas and source quotes do not independently prove semantic correctness. Site changes,
ambiguous feedback and noisy reflection remain reasons to inspect or retire an entry.
The harness retains immutable original evidence and recoverable context projections.

## Inspect, revalidate, retire and roll back

```powershell
python -m cdp_browser_agent.browser --config examples/playbook-30000.json --playbook-history ENTRY_ID
python -m cdp_browser_agent.browser --config examples/playbook-30000.json --playbook-replay ENTRY_ID --playbook-version 1 --replay-suite playbook-release-headings --revalidate
python -m cdp_browser_agent.browser --config examples/playbook-30000.json --playbook-retire ENTRY_ID --playbook-version 1
python -m cdp_browser_agent.browser --config examples/playbook-30000.json --playbook-rollback ENTRY_ID --playbook-version 2 --restore-version 1
```

`--revalidate` runs a fresh evaluation; otherwise an identical completed evaluation may
reuse its durable receipt. Revision checks prevent stale writes. Rollback needs a
previously validated, unexpired, superseded version and cannot undo explicit retirement.

MCP adds `browser_playbook_list`, `browser_playbook_read`, `browser_playbook_replay`,
`browser_playbook_retire` and `browser_playbook_rollback` (30 tools in total).
The inner planner receives only the read-only `playbook_list` inspection tool; adoption
and retirement are controlled by the harness/operator. An outer MCP caller can invoke
the management tools using its normal user authorization.

The store is `learning.state_dir/playbook.sqlite3` (default: the task-state directory's
`playbook/`). It retains versions, evidence, source lineage, leases and audit events.
Proposal and replay JSON receipts are also stored under `learning/` and `replays/`.
Processing outputs remain under the configured processing artifact root.
Browser results expose `playbook_learning` and `playbook_selections`; worker results
expose `playbook_entries` and the learning receipt after feedback.

## Research relationship

Inspired by [ACE, arXiv v3](https://arxiv.org/html/2510.04618v3), especially role separation,
itemized experience and incremental updates. The [author implementation](https://github.com/ace-agent/ace)
was inspected at `82709de050e1db6e6ef2f07bcb0393560b94992a`; its source is not vendored.
Version control, scope fingerprints, held-out adoption gates and rollback are this
project's engineering choices. No ACE benchmark score is claimed for this implementation.
