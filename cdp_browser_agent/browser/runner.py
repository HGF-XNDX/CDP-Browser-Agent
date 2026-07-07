from __future__ import annotations

import json
from pathlib import Path

from .agent import run_agent
from .resource_analyzer import configure_for_download


async def run_browser_agent(task: str, config: dict, shared_memory=None) -> dict:
    """Run the browser agent as a direct in-process function call (no subprocess)."""
    if shared_memory is not None:
        agent_settings = config.setdefault("agent", {})
        agent_settings["shared_events_path"] = str(shared_memory.events_path)
        agent_settings["shared_workflow_id"] = shared_memory.workflow_id
    return await run_agent(task, config)
