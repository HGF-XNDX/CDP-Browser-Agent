# Continuing workers and procedural advice

Use `browser_worker_start(profile, records)` when the result may need feedback. It
executes the first turn and returns `worker_session_id`, `turn`, status and export paths.
The configured method, source records and resolved model are frozen. A worker cannot
browse, run arbitrary scripts or delegate recursively.

Use `browser_worker_continue(worker_session_id, feedback, expected_turn)` with the
last observed turn to request a revision. Follow-up instructions must fit the frozen
method and checks. `applied_feedback` describes the last revision; if it already matches
the requested change and the worker is completed, inspect/deliver the result. Repeating
the same successful feedback is idempotent; stale turn numbers are rejected.

Call continue without feedback to resume an interrupted pending turn. Completed items
are reused after checking their receipts. New feedback cannot replace a pending turn;
resume it or explicitly cancel it first. `browser_worker_cancel` stops the current turn
and preserves earlier results. A cancelled turn restarts only through new feedback.
`busy` preserves a session ID: inspect it and continue once capacity is available.

`browser_worker_status(id, after)` returns paginated events and the current summary.
Use event_cursor as the next after value. Omitting the ID lists standalone workers.
Task-created children are visible inside their parent's context. Inside `browser_task`,
the corresponding tools are delegate_processing, processing_continue, processing_status
and processing_cancel. Keep parent task IDs and worker IDs distinct.

Inspect contract_verified, failed_count and exported evidence. The host can require a
specific profile, minimum records and minimum turn via agent.completion_processing.
Host-verified completion means those configured checks passed; it does not prove that
every summary or inference is semantically correct.

Successful feedback can produce candidate_experience_id. `browser_experience_list`
shows candidates and operator-registered replay suites. `browser_experience_replay`
calls the model for both baseline and candidate, using distinct held-out records with
operator expected outputs. The caller cannot supply arbitrary targets in this tool.
Only an all-passing candidate with at least one baseline failure is promoted. A successful
current task or model self-evaluation alone cannot promote advice. If no suitable suite
is registered, leave the advice as a candidate.

New processing jobs automatically recall promoted advice for exactly the same frozen
method; it expires after 30 days and `browser_experience_revoke` prevents future use.
This is scoped procedural memory, not model training or automatic Skill/code editing.
Auto replay is opt-in through processing.auto_replay; a failed replay does not erase a
completed delivery. Inspect the replay report before reporting an improvement.

CLI equivalents: --worker-start PROFILE --processing-input FILE; --worker-continue ID
with optional --worker-feedback TEXT --worker-turn N; --worker-status [ID];
--worker-cancel ID; --experience-list [PROFILE]; --experience-replay ID --replay-suite NAME;
--experience-revoke ID. All commands use the same operator --config.
