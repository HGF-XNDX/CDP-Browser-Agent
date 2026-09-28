from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import os

from .agent import run_agent
from ..harness.runtime import ExtensionRuntime
from ..harness.tools import Tool
from ..harness.session import RunSession
from ..harness.task_store import TaskStore
from ..model_client import RUN_METRICS


async def run_browser_agent(task: str | None, config: dict, shared_memory=None, *, tools: list[Tool] | None = None, completion_check=None,
                            resume_run_id=None, user_input=None) -> dict:
    """Run the browser agent as a direct in-process function call (no subprocess)."""
    if resume_run_id and task is None:
        store = TaskStore(config)
        try:
            task = store.get(resume_run_id)["task"]
        finally:
            store.close()
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

    session = RunSession(config, task, resume_run_id)
    state = session.state
    if resume_run_id:
        if state["status"] == "completed":
            session.finish()
            return state
        state.update(status="running", answer="", stopped_reason=None)
        if state.get("pending_action"):
            state["last_result"] = {"ok": False, "uncertain_action": state["pending_action"],
                "message": "Interrupted action may have completed. Inspect actual effects before deciding the next action."}
        if user_input:
            state.setdefault("decisions", []).append({"mode": "user", "answer": user_input, "reason": "resume_input"})
            state["last_result"] = {"ok": True, "user_response": user_input, "uncertain_action": state.get("pending_action")}
        state.pop("pending_input", None)
        session.checkpoint()

    async def execute():
        if model.get("apiKeyEnv"):
            model["apiKey"] = os.environ[model["apiKeyEnv"]]
        async with ExtensionRuntime(config, tools) as runtime:
            runtime.task_state = state
            for name, frozen in state.get("active_skills", {}).items():
                if runtime.skills.load(name) != frozen:
                    raise ValueError("An active skill changed since the checkpoint; start a new task to use the new instructions")
            return await run_agent(task, config, runtime, session=session, completion_check=completion_check)

    metrics_token = RUN_METRICS.set(state.setdefault("metrics", {}))
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
        try:
            session.finish()
        finally:
            RUN_METRICS.reset(metrics_token)
    return state
