from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import os

from .agent import run_agent
from ..harness.runtime import ExtensionRuntime
from ..harness.tools import Tool
from ..harness.session import RunSession


async def run_browser_agent(task: str, config: dict, shared_memory=None, *, tools: list[Tool] | None = None, completion_check=None) -> dict:
    """Run the browser agent as a direct in-process function call (no subprocess)."""
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    config = deepcopy(config)
    if not 1 <= int(config.get("agent", {}).get("max_steps", 40)) <= 1000:
        raise ValueError("max_steps must be between 1 and 1000")
    model = config.setdefault("model", {})
    if shared_memory is not None:
        agent_settings = config.setdefault("agent", {})
        agent_settings["shared_events_path"] = str(shared_memory.events_path)
        agent_settings["shared_workflow_id"] = shared_memory.workflow_id
    timeout = float(config.get("harness", {}).get("run_timeout_seconds", 600))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("run_timeout_seconds must be positive and finite")

    session = RunSession(config, task)
    state = session.state

    async def execute():
        if model.get("apiKeyEnv"):
            model["apiKey"] = os.environ[model["apiKeyEnv"]]
        async with ExtensionRuntime(config, tools) as runtime:
            return await run_agent(task, config, runtime, session=session, completion_check=completion_check)

    operation = asyncio.create_task(execute())
    try:
        done, _ = await asyncio.wait({operation}, timeout=timeout)
        if not done:
            state.update(status="timeout", stopped_reason="run_timeout",
                         answer=f"Run exceeded {timeout:g} seconds; task completion is unverified.")
            operation.cancel()
        try:
            await operation
        except asyncio.CancelledError:
            if state["status"] != "timeout":
                raise
    except asyncio.CancelledError:
        state.update(status="cancelled", stopped_reason="cancelled")
        operation.cancel()
        try:
            await operation
        except asyncio.CancelledError:
            pass
        raise
    except Exception as exc:
        state.update(status="failed", stopped_reason="runtime_error", answer=str(exc)[:2000])
        session.recorder.write("runtime_error", {"type": type(exc).__name__, "message": str(exc)[:2000]})
    finally:
        session.finish()
    return state
