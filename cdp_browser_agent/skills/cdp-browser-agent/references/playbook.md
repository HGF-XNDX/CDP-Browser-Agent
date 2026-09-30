# Procedural learning and experience management

Requires version 0.10+. Inspect `browser_capabilities.playbook_learning`; extra model
calls and suite paths are configured by the operator. Tasks can automatically reflect
on failures, and workers can learn from feedback continuations. No matching replay suite
means new lessons remain candidates. The outer agent does not need to manufacture advice.

Use `browser_playbook_list(offset, limit)` to inspect versions and available replay suites.
Use `browser_playbook_read(entry_id, version?)` for a version and its audit history.
Check `state`, scope, expiry, candidate/baseline results and evidence references before
describing an experience as adopted or effective. `active` means the configured replay
passed, not general factual correctness or guaranteed future success.

When the user requests evaluation, call `browser_playbook_replay(entry_id, version, suite)`.
This calls the configured model. Only operator-registered suites are accepted. Set
`force=true` when a fresh regression check is requested instead of reusing a receipt.
Ordinary planner suites verify decisions without executing proposed actions. Planner
suites with `evaluation: document_recipe` execute bounded document repairs on independent
source fixtures and verify the generated output. Processing suites run the processing
method and validate output artifacts and expected data.

When asked to stop using an experience, `browser_playbook_retire(entry_id, expected_version)`
retires all versions while retaining evidence. For a bad update,
`browser_playbook_rollback(entry_id, expected_version, restore_version)` restores a
previously validated, unexpired revision. Read the current version first to avoid stale
writes. Rollback cannot activate an untested candidate or undo explicit retirement.

Browser results expose `playbook_selections` and `playbook_learning`; workers expose
`playbook_entries`. Check these alongside actual task outputs to distinguish available
experience from experience selected by a task. Selection alone does not establish that
it caused an improvement. Learned advice cannot change task goals, permissions or skills.

Document runs can also expose `repair_reflections` and `repair_adoption`. These are
temporary, source-specific advice and actual subsequent action/output-change receipts.
They do not establish retained experience or a causal gain. An inconclusive transient
review can use the task's `document_review_retry` tool with the exact current review ID;
old attempts remain saved and unapproved candidates cannot export.
