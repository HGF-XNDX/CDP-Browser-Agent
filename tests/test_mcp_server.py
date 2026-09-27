import sys
from unittest.mock import AsyncMock

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from cdp_browser_agent import mcp_server
from cdp_browser_agent.browser.default_config import browser_agent_default_config


async def test_real_stdio_server_capabilities_and_argument_boundary():
    async with Client(StdioServerParameters(command=sys.executable, args=["-m", "cdp_browser_agent.mcp_server"])) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {"browser_task", "browser_capabilities", "browser_workflows", "browser_workflow_run", "browser_workflow_status", "browser_workflow_pause"}
        task = next(t for t in tools.tools if t.name == "browser_task")
        assert set(task.input_schema["properties"]) == {"task", "max_steps"}
        result = await client.call_tool("browser_capabilities", {})
        assert not result.is_error
        assert result.structured_content["version"] == "0.4.0"
        invalid = await client.call_tool("browser_task", {"task": " ", "max_steps": 1})
        assert invalid.is_error


async def test_in_process_task_respects_operator_config(monkeypatch):
    config = browser_agent_default_config()
    config["agent"]["max_steps"] = 7
    runner = AsyncMock(return_value={"status": "completed", "answer": "ok", "history": ["large"], "sources": []})
    monkeypatch.setattr(mcp_server, "run_browser_agent", runner)
    async with Client(mcp_server.create_mcp_server(config)) as client:
        invalid = await client.call_tool("browser_task", {"task": "inspect", "max_steps": 8})
        assert invalid.is_error
        runner.assert_not_awaited()
        result = await client.call_tool("browser_task", {"task": "inspect", "max_steps": 3})
        assert result.structured_content["status"] == "completed"
        assert "history" not in result.structured_content
        assert runner.call_args.args[1]["agent"]["max_steps"] == 3
        assert config["agent"]["max_steps"] == 7
