from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import logging
from typing import Any

from mcp.server import MCPServer

from . import __version__
from .browser.runner import run_browser_agent
from .configuration import load_config
from .harness.skills import SkillCatalog
from .workflows.runner import run_workflow
from .workflows.spec import WorkflowCatalog
from .workflows.store import WorkflowStore, WorkflowBusy
from .web.tools import WebTools, capabilities as web_capabilities
from .processing.engine import ProcessingEngine
from .harness.task_store import TaskStore
from .harness.artifacts import ArtifactStore
from pathlib import Path
from uuid import uuid4


def create_mcp_server(config: dict | None = None) -> MCPServer:
    """Operator-owned config: callers cannot select files, commands, endpoints or secrets."""
    base = deepcopy(config if config is not None else load_config())
    # CDP attaches to a shared browser; serialize tasks on this server instance.
    lock = asyncio.Lock()
    web = WebTools(base.get("web", {}))
    server = MCPServer("CDP Browser Agent", version=__version__, instructions=(
        "Use web_search/web_fetch for fast public reading without a model or browser, "
        "and browser_task for interactive tasks or web-tool fallback. Inspect status: completed means the "
        "planner reported completion; needs_input requires user action. Model/browser/skill/MCP "
        "connections are configured by the server operator, not tool arguments."))

    @server.tool()
    async def browser_capabilities() -> dict[str, Any]:
        """Inspect configuration and skill metadata without opening a browser or calling a model."""
        settings = base.get("harness", {})
        skills = SkillCatalog(settings.get("skill_paths", []), int(settings.get("max_skill_chars", 20000)),
                              int(settings.get("active_skill_budget_chars", 30000)))
        return {"version": __version__, "connection": base.get("browser", {}).get("connection", "launch"),
                "max_steps": base.get("agent", {}).get("max_steps", 40),
                "skills": skills.catalog(), "active_skills": settings.get("active_skills", []),
                "configured_mcp_servers": list(settings.get("mcp_servers", {})),
                "web": web_capabilities(base.get("web", {})),
                "processing_profiles": ProcessingEngine(base).catalog.catalog(),
                "intervention": base.get("intervention", {}),
                "workflows": WorkflowCatalog(base.get("workflows", {}).get("paths", [])).catalog(),
                "external_connections_verified": False}

    @server.tool()
    async def browser_task(task: str, max_steps: int | None = None) -> dict[str, Any]:
        """Run a browser task; return status, answer, artifacts, sources, and the run log path.

        max_steps may lower (never raise) the operator's step limit. needs_input returns
        immediately; it does not block the MCP process or imply resumable browser state.
        """
        if not task.strip():
            raise ValueError("task is required")
        config = deepcopy(base)
        limit = int(config.get("agent", {}).get("max_steps", 40))
        if max_steps is not None:
            if not 1 <= max_steps <= limit:
                raise ValueError(f"max_steps must be between 1 and {limit}")
            config.setdefault("agent", {})["max_steps"] = max_steps
        if lock.locked():
            return {"status": "busy", "answer": "This browser server already has a running task."}
        async with lock:
            result = await run_browser_agent(task, config)
        # Detailed observations/history stay in the run log rather than bloating the host's context.
        keys = ("run_id", "status", "stopped_reason", "answer", "step", "completion_basis", "browser_started", "collected_files", "log_file", "processing_results", "decisions", "metrics", "learning", "context_budget", "last_compaction", "context_view")
        return {**{key: result[key] for key in keys if key in result},
                "sources": [{"url": s["url"], "title": s.get("title", ""), "kind": s.get("kind", "page")} for s in result.get("sources", [])]}

    @server.tool()
    async def browser_task_resume(run_id: str, user_input: str | None = None) -> dict[str, Any]:
        """Resume a persisted task using the same operator configuration and refreshed page state."""
        if lock.locked():
            return {"status": "busy"}
        async with lock:
            result = await run_browser_agent(None, base, resume_run_id=run_id, user_input=user_input)
        return {k: result.get(k) for k in ("run_id", "status", "answer", "step", "sources", "collected_files", "decisions", "metrics", "context_budget", "last_compaction", "context_view")}

    @server.tool()
    async def browser_task_status(run_id: str | None = None) -> dict[str, Any]:
        """Inspect task progress/pending input, or list recent tasks when run_id is omitted."""
        store = TaskStore(base)
        try:
            if run_id is None:
                return {"tasks": store.list()}
            result = store.get(run_id)
            return {k: result.get(k) for k in ("run_id", "status", "step", "pending_input", "answer", "plan", "metrics", "context_budget", "last_compaction", "context_view")}
        finally:
            store.close()

    def run_artifacts(run_id):
        store = TaskStore(base)
        try:
            store.get(run_id)  # Validate identity and existence before constructing paths.
            return ArtifactStore(store.root / run_id / "artifacts")
        finally:
            store.close()

    @server.tool()
    async def browser_artifact_read(run_id: str, artifact_id: str, offset: int = 0, limit: int = 4000) -> dict[str, Any]:
        """Read original evidence from a known task by hash ID; offsets count JSON characters. No arbitrary paths."""
        return run_artifacts(run_id).read(artifact_id, offset, limit)

    @server.tool()
    async def browser_artifact_search(run_id: str, artifact_id: str, query: str, offset: int = 0, limit: int = 10) -> dict[str, Any]:
        """Search literal text in a task's saved evidence; use returned offsets with browser_artifact_read."""
        return run_artifacts(run_id).search(artifact_id, query, offset, limit)

    @server.tool()
    async def browser_task_respond(run_id: str, request_id: str, answer: str) -> dict[str, Any]:
        """Supply actual user input to a currently waiting task. Does not restart or duplicate it."""
        store = TaskStore(base)
        try:
            return store.respond(run_id, request_id, answer)
        finally:
            store.close()

    @server.tool()
    async def browser_process(profile: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Process supplied data with an operator-defined subagent method/schema and export JSON/CSV/Markdown.

        records are [{data: {...}, source_url: optional URL}]. Source content is supplied by
        the caller; schema/quote checks do not independently verify its factual accuracy.
        """
        engine = ProcessingEngine(base)
        engine.catalog.get(profile)
        output = Path(base.get("processing", {}).get("artifact_dir", "downloads/processed")) / uuid4().hex
        return await engine.run(profile, records, output)

    @server.tool()
    async def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
        """Find public webpages quickly, without a model/browser. Fetch promising URLs for full text.

        Network/proxy/provider configuration is operator-owned. Search snippets are leads,
        not verified page content. needs_browser/browser_url describe an interactive fallback.
        """
        return await web.search(query, max_results)

    @server.tool()
    async def web_fetch(url: str, offset: int = 0, max_chars: int = 6000) -> dict[str, Any]:
        """Read public HTML/text, with source files and offsets. Does not use browser login cookies.

        On needs_browser=true, use browser_task for the requested page. Restricted URLs and
        disabled capabilities are not authorization to bypass policy using another tool.
        """
        return await web.fetch(url, offset, max_chars)

    @server.tool()
    async def browser_workflows() -> dict[str, Any]:
        """List operator-configured website workflows and their parameter schemas."""
        return {"workflows": WorkflowCatalog(base.get("workflows", {}).get("paths", [])).catalog()}

    @server.tool()
    async def browser_workflow_run(name: str, parameters: dict[str, Any] | None = None, resume_run_id: str | None = None,
                                   page_budget: int | None = None, retry_uncertain_step: bool = False) -> dict[str, Any]:
        """Run a registered collection workflow. Resume with its run ID; do not change its parameters.

        page_budget pauses cooperatively. retry_uncertain_step must be an explicit
        caller decision after inspecting a previously interrupted agent step's effects.
        completed is checked against workflow conditions, not a claim of full-site coverage.
        """
        if lock.locked():
            return {"status": "busy", "answer": "This browser server already has a running task."}
        async with lock:
            try:
                return await run_workflow(name, base, parameters, resume_run_id=resume_run_id, page_budget=page_budget,
                                           retry_uncertain_step=retry_uncertain_step)
            except WorkflowBusy as exc:
                return {"status": "busy", "answer": str(exc)}

    @server.tool()
    async def browser_workflow_status(run_id: str) -> dict[str, Any]:
        """Read persisted workflow progress without opening a browser or calling a model."""
        store = WorkflowStore(base.get("workflows", {}).get("state_dir", "workflow-runs"))
        try:
            return store.result(store.get(run_id))
        finally:
            store.close()

    @server.tool()
    async def browser_workflow_pause(run_id: str) -> dict[str, Any]:
        """Request pause at the next checkpoint; does not interrupt an in-flight page or tool."""
        store = WorkflowStore(base.get("workflows", {}).get("state_dir", "workflow-runs"))
        try:
            return store.request_pause(run_id)
        finally:
            store.close()

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Expose the general browser agent over MCP.")
    parser.add_argument("--config", help="Operator-owned JSON configuration, resolved relative to this file.")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport != "stdio" and args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The bundled unauthenticated server binds to loopback only; deploy an authenticated host for remote use.")
    logging.basicConfig(level=logging.INFO)
    server = create_mcp_server(load_config(args.config))
    options = {} if args.transport == "stdio" else {"host": args.host, "port": args.port}
    server.run(args.transport, **options)


if __name__ == "__main__":
    main()
