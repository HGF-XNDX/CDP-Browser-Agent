# Repeatable website collection

Use a registered workflow when the same website and fields will be collected repeatedly.
Inspect `browser_workflows`, then call `browser_workflow_run` with its name and schema-valid parameters.
Read `status`, `completion_basis`, `record_count`, `step_results`, `changes`, and the output files.
`coverage: bounded` means an intentionally limited sample, not an exhausted website.

CLI equivalents:

```text
python -m cdp_browser_agent.browser --config CONFIG --list-workflows
python -m cdp_browser_agent.browser --config CONFIG --workflow NAME --page-budget 1
python -m cdp_browser_agent.browser --config CONFIG --workflow NAME --resume-run RUN_ID
python -m cdp_browser_agent.browser --config CONFIG --workflow-status RUN_ID
```

Resume a paused/interrupted run only with the same definition and parameters. A new run compares
with the most recent completed matching run. Changes are new/changed/unchanged, not inferred deletions.
Do not automatically pass `retry_uncertain_step`: an interrupted agent step may already have effects.
Inspect its event log first. Browser authentication is separate from the collection checkpoint;
use an operator-prepared CDP session for sites requiring human login.

When authoring workflows, use observed DOM selectors, stable keys, explicit required fields, and
host checks for agent preparation steps. Keep site rules in the workflow, not the general browser core.
Navigate and crawl deterministic pages without model calls; use agent steps only for page preparation.
Link pagination is supported; JS-only pagination and transient POST filter state need additional adaptation.
Never claim results from a login page or empty loading shell. Source snapshots live in
`snapshots/<sha256>.html`, keyed by the page/detail hashes in records.
