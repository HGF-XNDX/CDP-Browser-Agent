# CDP Browser Agent

[English](README.md) | [中文](README.zh-CN.md)

Standalone AI browser agent extracted from the UnifiedAgent project.

It uses Playwright for browser control and any OpenAI-compatible chat-completions
endpoint for planning browser actions. It can launch Chromium itself or connect
to an existing Chrome/Edge session through CDP.

## Install

Install directly from GitHub:

```powershell
pip install git+https://github.com/HGF-XNDX/CDP-Browser-Agent.git
python -m playwright install chromium
```

Install a specific branch or commit:

```powershell
pip install git+https://github.com/HGF-XNDX/CDP-Browser-Agent.git@main
```

For local development:

```powershell
git clone https://github.com/HGF-XNDX/CDP-Browser-Agent.git
cd CDP-Browser-Agent
pip install -e .
python -m playwright install chromium
```

After the package is published to PyPI, this will also work:

```powershell
pip install cdp-browser-agent
python -m playwright install chromium
```

## Quick Start

Run against a local OpenAI-compatible endpoint:

```powershell
browser-agent `
  --base-url http://127.0.0.1:8080/v1 `
  --model local-model `
  --headless `
  --max-steps 20 `
  "Open https://example.com and summarize the visible page"
```

Connect to an existing CDP browser:

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir=D:\chrome-cdp-profile

browser-agent `
  --connection cdp `
  --cdp-url http://127.0.0.1:9222 `
  --base-url http://127.0.0.1:8080/v1 `
  "Search the web for Playwright Python documentation and open the official page"
```

## Python API

```python
import asyncio

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent


async def main():
    config = browser_agent_default_config()
    config["model"]["baseUrl"] = "http://127.0.0.1:8080/v1"
    config["browser"]["headless"] = True
    config["agent"]["max_steps"] = 20

    result = await run_browser_agent(
        "Open https://example.com and summarize the visible page",
        config,
    )
    print(result)


asyncio.run(main())
```

## MCP Server

This package can also run as an MCP server. MCP clients can call the
`browser_task` tool to let the agent operate a browser.

Start the MCP server over stdio:

```powershell
cdp-browser-agent-mcp
```

Equivalent module form:

```powershell
python -m cdp_browser_agent.mcp_server
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "cdp-browser-agent": {
      "command": "cdp-browser-agent-mcp",
      "args": []
    }
  }
}
```

If your MCP client cannot find console scripts, use Python directly:

```json
{
  "mcpServers": {
    "cdp-browser-agent": {
      "command": "python",
      "args": ["-m", "cdp_browser_agent.mcp_server"]
    }
  }
}
```

The exposed MCP tool is:

- `browser_task`: run a natural-language browser task.

Common tool arguments:

- `task`: required natural-language browser task.
- `base_url`: OpenAI-compatible `/v1` endpoint, for example `http://127.0.0.1:8080/v1`.
- `connection`: `launch` or `cdp`.
- `cdp_url`: CDP endpoint when using `connection="cdp"`.
- `headless`: whether to run the browser headless.
- `max_steps`: maximum browser planning steps.
- `config_path`: optional JSON config overlay path.

You can also run HTTP transports for clients that support them:

```powershell
cdp-browser-agent-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

## Configuration

You can pass a JSON overlay with `--config`. The file is deep-merged onto the
default config.

```powershell
browser-agent --config examples/local-llamacpp.json "Open https://example.com"
```

Important fields:

- `model.baseUrl`: OpenAI-compatible `/v1` endpoint.
- `model.model`: model name. Leave empty to auto-discover from `/props` or `/models`.
- `model.enableThinking`: enables model thinking for backends that support it.
- `browser.connection`: `launch` or `cdp`.
- `browser.downloads_path`: where downloaded files are stored.
- `agent.max_steps`: maximum browser planning steps.
- `agent.strategy_evaluator_enabled`: enables a second model pass to review proposed actions.
- `agent.browser_site_memory_enabled`: stores cross-run site interaction memory.

## Notes

This package currently preserves a few battle-tested behaviors from the source
project, including guarded download handling, no-progress detection, site memory,
and deterministic handling for some official download pages. Those rules are
implemented as browser-agent safety and reliability features; domain-specific
data processing should live in a separate package or plugin.

## Publish

```powershell
python -m pip install build twine
python -m build
python -m twine check dist/*
```

Then publish to your package index when ready.
