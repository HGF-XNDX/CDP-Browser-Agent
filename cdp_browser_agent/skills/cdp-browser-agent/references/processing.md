# Processing and task continuation

Inspect `browser_capabilities.processing_profiles` before choosing a profile. The operator
registers methods, Skill names, model configuration, JSON Schema and export formats;
MCP arguments cannot create new methods or arbitrary executable workers.

For repeatable collection use a registered `fetch/crawl → process` workflow. A process
step names `input_step` and `profile`. Ordinary `browser_task` can use `delegate_processing`
after gathering page sources; search snippets alone are not processing evidence. If
full browser-page content is required, use a suitable collection workflow rather than
assuming a short observation captured the entire document.

For caller-supplied records:

```json
{"profile":"page-facts","records":[{"source_url":"https://example.com","data":{"title":"Example Domain","text":"Actual collected text"}}]}
```

Use `browser_process` and inspect the result's status, validated_count, failed_count,
records_path and artifact_paths. The canonical JSON keeps source identities and quotes;
CSV/Markdown are convenient views. Input size limits fail explicitly. Successful items
are reusable on workflow resume; failed items retain their attempts. Changing method,
model or Skill requires a new processing run. This worker has no browser, shell or
recursive delegation. Source quote matching is not independent semantic verification.

Use `browser_task_status(run_id)` for progress, plan and pending input. Omit run_id to
list recent tasks. Respond to a live `waiting_input` request using its exact request ID
and the user's actual answer. The operator's auto/wait policy handles missing answers;
do not reinterpret a timeout as a user approval. For a stopped task use
`browser_task_resume(run_id, user_input?)`, keeping its original operator configuration.
Active tasks return busy/lease conflict. Ordinary tasks and workflows have distinct IDs
and distinct resume tools; preserve the appropriate ID from the result.

The CLI equivalents are `--task-status`, `--task-respond --request-id --user-input`,
`--resume-task`, and `--process-profile --processing-input`. Processing input is a JSON
array of records. Always use the package's configured Python environment.
