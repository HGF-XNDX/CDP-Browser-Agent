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


def create_mcp_server(config: dict | None = None) -> MCPServer:
    """Operator-owned config: callers cannot select files, commands, endpoints or secrets."""
    base = deepcopy(config if config is not None else load_config())
    # CDP attaches to a shared browser; serialize tasks on this server instance.
    lock = asyncio.Lock()
    server = MCPServer("CDP Browser Agent", version=__version__, instructions=(
        "Use browser_task for a bounded browser task. Inspect status: completed means the "
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
        keys = ("run_id", "status", "stopped_reason", "answer", "step", "completion_basis", "collected_files", "log_file")
        return {**{key: result[key] for key in keys if key in result},
                "sources": [{"url": s["url"], "title": s.get("title", "")} for s in result.get("sources", [])]}

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
