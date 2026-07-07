from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Literal

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - exercised only in incomplete installs.
    raise SystemExit(
        "MCP support requires the 'mcp' package. Install with: "
        "pip install mcp"
    ) from exc

from .browser.default_config import browser_agent_default_config, deep_merge_config
from .browser.runner import run_browser_agent


def _load_config(path: str | None) -> dict[str, Any]:
    config = browser_agent_default_config()
    if not path:
        return config
    override_path = Path(path).expanduser()
    override = json.loads(override_path.read_text(encoding="utf-8"))
    if not isinstance(override, dict):
        raise ValueError("config file must contain a JSON object")
    return deep_merge_config(config, override)


def _apply_overrides(
    config: dict[str, Any],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    thinking: bool | None = None,
    connection: Literal["launch", "cdp"] | None = None,
    cdp_url: str | None = None,
    start_url: str | None = None,
    downloads_path: str | None = None,
    headless: bool | None = None,
    max_steps: int | None = None,
    strategy_evaluator: bool | None = None,
    site_memory: bool | None = None,
    analyze_downloads: bool | None = None,
) -> dict[str, Any]:
    model_cfg = config.setdefault("model", {})
    browser_cfg = config.setdefault("browser", {})
    agent_cfg = config.setdefault("agent", {})

    if base_url is not None:
        model_cfg["baseUrl"] = base_url
    if model is not None:
        model_cfg["model"] = model
    if api_key is not None:
        model_cfg["apiKey"] = api_key
    if provider is not None:
        model_cfg["provider"] = provider
    if max_tokens is not None:
        model_cfg["maxTokens"] = max_tokens
    if temperature is not None:
        model_cfg["temperature"] = temperature
    if thinking is not None:
        model_cfg["enableThinking"] = thinking

    if connection is not None:
        browser_cfg["connection"] = connection
    if cdp_url is not None:
        browser_cfg["cdp_url"] = cdp_url
    if start_url is not None:
        browser_cfg["start_url"] = start_url
    if downloads_path is not None:
        browser_cfg["downloads_path"] = downloads_path
    if headless is not None:
        browser_cfg["headless"] = headless

    if max_steps is not None:
        agent_cfg["max_steps"] = max_steps
    if strategy_evaluator is not None:
        agent_cfg["strategy_evaluator_enabled"] = strategy_evaluator
    if site_memory is not None:
        agent_cfg["browser_site_memory_enabled"] = site_memory
    if analyze_downloads is not None:
        agent_cfg["analyze_downloads_after_run"] = analyze_downloads
        agent_cfg["skip_download_model_summary"] = not analyze_downloads
    return config


def create_mcp_server(host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    mcp = FastMCP(
        "CDP Browser Agent",
        host=host,
        port=port,
        instructions=(
            "Run browser automation tasks through Playwright using an "
            "OpenAI-compatible chat model. Use browser_task for web navigation, "
            "page inspection, downloading public resources, and summarizing "
            "browser-observed evidence."
        ),
    )

    @mcp.tool()
    async def browser_task(
        task: str,
        config_path: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: bool | None = None,
        connection: Literal["launch", "cdp"] | None = None,
        cdp_url: str | None = None,
        start_url: str | None = None,
        downloads_path: str | None = None,
        headless: bool | None = None,
        max_steps: int | None = None,
        strategy_evaluator: bool | None = None,
        site_memory: bool | None = None,
        analyze_downloads: bool | None = None,
    ) -> dict[str, Any]:
        """Run a natural-language browser task and return the agent result.

        The tool starts from the package default config, optionally deep-merges a
        JSON config file, then applies explicit arguments. For local llama.cpp,
        pass base_url like http://127.0.0.1:8080/v1.
        """

        if not task or not task.strip():
            raise ValueError("task is required")
        config = _apply_overrides(
            _load_config(config_path),
            base_url=base_url,
            model=model,
            api_key=api_key,
            provider=provider,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            connection=connection,
            cdp_url=cdp_url,
            start_url=start_url,
            downloads_path=downloads_path,
            headless=headless,
            max_steps=max_steps,
            strategy_evaluator=strategy_evaluator,
            site_memory=site_memory,
            analyze_downloads=analyze_downloads,
        )
        # MCP stdio uses stdout for JSON-RPC frames. Keep browser-agent progress
        # logs away from stdout so MCP clients can parse the protocol safely.
        with contextlib.redirect_stdout(sys.stderr):
            return await run_browser_agent(task, config)

    return mcp


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cdp-browser-agent-mcp",
        description="Run CDP Browser Agent as an MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport. Most desktop MCP clients use stdio.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transports.")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transports.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    server = create_mcp_server(host=args.host, port=args.port)
    server.run(args.transport)


if __name__ == "__main__":
    main()
