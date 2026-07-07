# CDP Browser Agent

Standalone AI browser agent extracted from the UnifiedAgent project.

It uses Playwright for browser control and any OpenAI-compatible chat-completions
endpoint for planning browser actions. It can launch Chromium itself or connect
to an existing Chrome/Edge session through CDP.

## Install

```powershell
pip install -e .
python -m playwright install chromium
```

For a published package:

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
