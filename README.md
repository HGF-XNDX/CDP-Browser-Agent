# CDP Browser Agent

English | [中文](README.zh-CN.md)

A general-purpose browser agent using Playwright and an OpenAI-compatible Chat
Completions endpoint. Launch Chromium or attach to Chrome/Edge over CDP. Load
Agent Skills, call external MCP tools, expose the agent as an MCP server, or
export its bundled Skill for another agent host.

Version 0.10 adds ACE-inspired procedural learning: automatic reflection and curation,
versioned scoped playbooks, paired held-out replay before adoption, expiry, retirement
and rollback. Browser planners and processing workers can reuse verified lessons.
The 30000 harness example enables candidate learning; the dedicated
`examples/playbook-30000.json` demonstrates automatic replay and adoption in a registered
workflow. See [learning configuration and boundaries](docs/PLAYBOOK_LEARNING.md) and
[validation evidence](docs/PLAYBOOK_VALIDATION.md). MCP now exposes 36 tools.

Full-document transformation uses generic inspection, embedded-document decoding,
planner-authored CSS/XPath/regex recipes, previews and replay-verified JSON exports.
No site-specific parser or task-specific Skill is required. All source structure,
including unselected tables and notes, is retained for review. See the
[document tools and diagnostic workflow](docs/DOCUMENTS.md) and [live validation boundaries](docs/DOCUMENT_VALIDATION.md). The earlier patent
adapter was removed because adapter success did not establish autonomous processing.

The current document evaluation configuration is [documents-gpt-6-luna.json](examples/documents-gpt-6-luna.json),
using `http://localhost:15536/v1`, `gpt-6-luna`, and the `CDP_BROWSER_AGENT_API_KEY`
environment variable. [Recovery validation](docs/DOCUMENT_RECOVERY_VALIDATION.md)
separates component checks, actual tool access, and completed agent tasks.

Version 0.9 adds bounded HTTP crawling: persistent URL queues, static pagination,
CSS field extraction, robots rules, shared origin throttling, retry/backoff, immutable
source records and JSON/JSONL/CSV exports. `web_crawl` can hand its complete dataset
to a processing worker by ID; HTTP crawling uses no per-page model calls or browser.
Use workflow `http_crawl` for repeatable collection and the existing browser `crawl`
for rendered pages. See the [crawler guide and design references](docs/CRAWLING.md)
and [acceptance evidence](docs/CRAWLER_VALIDATION.md).

Version 0.8 adds durable processing conversations, feedback revisions, cancellation,
operator-checked task completion, and paired held-out replay before processing advice
can be reused. CLI and MCP share the same persistent worker state. See the
[worker and learning guide](docs/CONTINUING_WORKERS.md) and [validation](docs/WORKER_VALIDATION.md).

Version 0.7 discovers active model context capacity, shares one token budget across planner/memory/workers, retains oversized tool results with read/search references, and commits recoverable prompt projections. Explicit smaller limits still apply. See [context and evidence management](docs/CONTEXT_HARNESS.md).

Version 0.6 adds independent data-processing workers with configurable methods, Skills,
output schemas and JSON/CSV/Markdown exports; persistent task resume; bounded human
waiting with autonomous continuation; evidence-linked reflection and verified procedural
memory. See the [usage guide](docs/PROCESSING_AUTONOMY.md),
[harness/context review](docs/HARNESS_EVOLUTION.md), and [acceptance evidence](docs/HARNESS_VALIDATION.md).

```powershell
python -m cdp_browser_agent.browser --config examples/harness-30000.json --workflow collect-and-process
```

Version 0.5 adds free web search, fast public-page fetching, automatic proxy discovery
for both tools, and lazy browser startup. Use the browser when static reading needs
interaction. See the [web tools guide](docs/WEB_TOOLS.md).

Version 0.4 introduced reusable website workflows: verified page preparation, deterministic
list/detail extraction, pagination, durable checkpoints, snapshots, and change detection.
See [workflow guide](docs/WORKFLOWS.md), [validation](docs/WORKFLOW_VALIDATION.md), and
[earlier upgrade notes](docs/UPGRADE.md).

```powershell
python -m cdp_browser_agent.browser --config examples/harness-30000.json --list-workflows
python -m cdp_browser_agent.browser --config examples/harness-30000.json --workflow example-domain
```

MCP also exposes `browser_workflows`, `browser_workflow_run`, `browser_workflow_status`,
and `browser_workflow_pause`. Workflows are operator-registered JSON files. Site-specific
examples are separately registered through `examples/ip-collection-30000.json`.

## Install and run

Python 3.10+:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser --headless `
  --base-url http://127.0.0.1:8080/v1 --model local-model `
  "Open https://example.com and summarize the page"
```

Configure a real model endpoint before running. This package does not deploy a
model. Console aliases: `browser-agent`, `cdp-browser-agent`. CLI stdout is JSON;
progress goes to stderr. The default start page is blank.

## Skills and external capabilities

```powershell
browser-agent --config examples/harness.json --list-skills
browser-agent --config examples/harness.json "Extract facts from https://example.com"
browser-agent --skill-path D:/skills --skill my-skill "The requested task"
```

`harness.skill_paths` lists skill folders or their parents. Metadata is discovered
first; `skill_load` activates instructions, `skill_read` retrieves bounded UTF-8
resources, and `skill_unload` releases context. `harness.active_skills` preloads
selected skills. Duplicate names and resource path escapes are rejected.
Scripts may be read, but automatic script execution is not provided. Use an
explicitly registered Python or MCP tool for execution. Skill metadata cannot
expand tool permissions.

[harness-with-mcp.json](examples/harness-with-mcp.json) configures a working local
text processing server. Its executable path assumes the Windows `.venv` above.
External servers support `stdio` and `streamable-http`; each requires an exact
`allow_tools` list. An empty list disables the server. Tools are namespaced as
`mcp.<server>.<tool>` and discovered with `tool_list` / `tool_describe`.
Arguments are validated against JSON Schema; calls are bounded and never retried
automatically. Large results are explicitly truncated.
Optional MCP servers fail independently; set `required: true` to make a connection
failure abort the task. Source identity and artifact references survive result compaction.

```json
{
  "harness": {
    "mcp_servers": {
      "lookup": {
        "transport": "streamable-http",
        "url": "http://127.0.0.1:9000/mcp",
        "allow_tools": ["lookup"],
        "headers_from_env": {"Authorization": "LOOKUP_AUTHORIZATION"}
      }
    }
  }
}
```

Set the full authorization value in the named environment variable. For stdio,
use `env_from` to pass selected environment variables. Config paths resolve
relative to the JSON file, independent of the MCP host's working directory.
Model credentials can be supplied via `model.apiKeyEnv`.

Python callers can use `run_browser_agent(task, config, tools=[...])` with async
`Tool(name, description, input_schema, handler)` objects from
`cdp_browser_agent.harness`. See the Chinese README for a complete example.

## MCP server

```powershell
cdp-browser-agent-mcp --config examples/harness.json
cdp-browser-agent-mcp --config examples/harness.json --transport streamable-http --port 8000
```

Tools include `browser_capabilities()`, `browser_task(task, max_steps?)`, the four workflow
tools, and `web_search(query, max_results?)` / `web_fetch(url, offset?, max_chars?)`.
Version 0.6 also exposes `browser_process`, `browser_task_resume`, `browser_task_status`,
and `browser_task_respond`; the current MCP server exposes 36 tools across browser,
web, workflow, crawl, processing, document and learning capabilities.
Direct web calls require neither a model call nor a running browser.
The operator owns configuration; callers cannot change file paths, model
endpoints, subprocess commands or credentials. `max_steps` can only lower the
operator's limit. Concurrent tasks return `busy` to avoid sharing a CDP page.
The HTTP launcher only binds to loopback; remote hosting needs authentication.

```json
{
  "mcpServers": {
    "cdp-browser-agent": {
      "command": "/absolute/path/to/venv/python",
      "args": ["-m", "cdp_browser_agent.mcp_server", "--config", "/absolute/path/to/config.json"]
    }
  }
}
```

## Export as a Skill

```powershell
browser-agent --export-skill .agents/skills
```

The [bundled Skill](cdp_browser_agent/skills/cdp-browser-agent/SKILL.md) is included
in the wheel. Export refuses to overwrite an existing skill. It delegates to
this package's MCP or CLI and still requires an independently configured model.

## Outcomes and verification

Runs return `completed`, `incomplete`, `blocked`, `needs_input`, `max_steps`, `stalled`, `failed`, or
`timeout`. Completion is explicitly `model_reported`, not independent proof of
success. Review the observed sources, artifacts and UUID JSONL log. Saving one
file does not terminate a multi-step task. `intervention.mode` selects `auto` (default),
bounded `wait` with autonomous continuation, or immediate `return` with a checkpoint.
Timeouts can leave side effects; verify them before repeating an action.

Launched Chromium is closed at exit; attached CDP browsers remain open. There is
durable task resume and browser storage snapshots, but no live DOM/JS heap restoration,
arbitrary script sandbox, or multi-tenant browser isolation. Native select/checkbox controls and basic iframe observations/actions are
supported. Complex nested/cross-origin frames, Canvas and uploads remain unverified.
In 0.3, custom planners must include `outcome=completed|incomplete|blocked` in `done`.
Cross-run site memory, second-pass strategy evaluation and model summaries are
opt-in. Local logs can contain page and task data.
Separate procedural experience is only recalled after two independent host-verified
runs; ordinary model-reported success does not promote it. This is not model training
or proof of improved task success rates.

```powershell
python -m pytest -q
python -m pip check
python -m build --no-isolation
python -m twine check dist/*
```

Tests cover actual stdio/HTTP MCP and real Chromium with a local model protocol
stub. They verify the skill/tool/browser pipeline, not real-model intelligence
or success rates on arbitrary websites.

A separate real-model evaluation against localhost:30000 improved independent
acceptance from 4/6 to 6/6 on the same small fixture suite, plus one real MCP → CDP
acceptance run. See [evidence and refactor review](docs/LIVE_TEST_30000.md) and
[ready-to-use model configuration](examples/harness-30000.json). These are bounded
engineering checks, not a general browser benchmark.
