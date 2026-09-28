# Context capacity and original evidence

Version 0.7 discovers active capacity from the configured model endpoint. The task's
`context_budget` reports actual capacity (if known), effective window, output reservation,
prompt allowance and discovery source. `model_capacity_tokens=null` means unknown;
the conservative fallback is 32768. Operator limits can be lower than actual capacity.
The 30000 example uses automatic discovery; do not hardcode a model's training window.

Large tool outputs carry `artifact.artifact_id`. Within the browser task use
`artifact_search(artifact_id, query)` for a literal match and `artifact_read(artifact_id,
offset, limit)` for slices. From the outer MCP host use `browser_artifact_search` or
`browser_artifact_read` with the task's `run_id` as well. These read only that run's
saved artifacts, never arbitrary paths. Offsets count characters in serialized JSON,
not bytes or offsets in a remote page. Follow `next_offset` for complete reading.

`last_compaction.original` identifies the pre-prune model view; a committed receipt
also identifies its projection. A full browser observation or full tool result may
have its own nested artifact reference. Follow those references for original evidence.
Do not repeat a tool with side effects just to recover text omitted from the prompt.
Hash mismatch or a missing artifact is an evidence error, not a successful retrieval.

Task, plan, user decisions, active skills and tool schemas stay pinned while redundant
history and large evidence blocks may be replaced by references. Stored sources remain
untrusted content. A summary or successful compaction does not verify task completion.

On `stopped_reason=context_budget`, inspect which operator limit or required prompt
prevented fitting. A provider overflow permits one smaller committed projection before
retry; unshrinkable instructions stop with an incomplete result. Processing workers
keep full inputs and fail an oversized record explicitly, so split records or select
fields through the processing method instead of assuming silent truncation occurred.
