from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .default_config import browser_agent_default_config, deep_merge_config
from .runner import run_browser_agent


def _load_config(path: str | None) -> dict[str, Any]:
    config = browser_agent_default_config()
    if not path:
        return config
    override = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(override, dict):
        raise ValueError("config file must contain a JSON object")
    return deep_merge_config(config, override)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="browser-agent",
        description="Run the standalone browser agent against an OpenAI-compatible model endpoint.",
    )
    parser.add_argument("task", nargs="?", help="Natural-language browser task.")
    parser.add_argument("--task", dest="task_option", help="Natural-language browser task; overrides positional task.")
    parser.add_argument("--config", help="Optional JSON config overlay.")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8080/v1.")
    parser.add_argument("--model", default=None, help="Model name. Empty means auto-discover/fallback.")
    parser.add_argument("--api-key", default=None, help="API key for compatible endpoints.")
    parser.add_argument("--provider", default=None, help="Provider label, default llama.cpp.")
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--thinking", action="store_true", help="Enable model thinking when supported.")
    parser.add_argument("--connection", choices=["launch", "cdp"], default=None)
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--start-url", default=None)
    parser.add_argument("--downloads-path", default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--no-strategy-evaluator", action="store_true")
    parser.add_argument("--no-site-memory", action="store_true")
    parser.add_argument("--analyze-downloads", action="store_true")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    task = args.task_option or args.task
    if not task:
        raise SystemExit("error: provide a task as positional text or --task")

    config = _load_config(args.config)
    model = config.setdefault("model", {})
    browser = config.setdefault("browser", {})
    agent = config.setdefault("agent", {})

    if args.base_url is not None:
        model["baseUrl"] = args.base_url
    if args.model is not None:
        model["model"] = args.model
    if args.api_key is not None:
        model["apiKey"] = args.api_key
    if args.provider is not None:
        model["provider"] = args.provider
    if args.max_tokens is not None:
        model["maxTokens"] = args.max_tokens
    if args.temperature is not None:
        model["temperature"] = args.temperature
    if args.thinking:
        model["enableThinking"] = True

    if args.connection is not None:
        browser["connection"] = args.connection
    if args.cdp_url is not None:
        browser["cdp_url"] = args.cdp_url
    if args.start_url is not None:
        browser["start_url"] = args.start_url
    if args.downloads_path is not None:
        browser["downloads_path"] = args.downloads_path
    if args.headless:
        browser["headless"] = True
    if args.headed:
        browser["headless"] = False

    if args.max_steps is not None:
        agent["max_steps"] = args.max_steps
    if args.log_dir is not None:
        agent["log_dir"] = args.log_dir
    if args.no_strategy_evaluator:
        agent["strategy_evaluator_enabled"] = False
    if args.no_site_memory:
        agent["browser_site_memory_enabled"] = False
    if args.analyze_downloads:
        agent["analyze_downloads_after_run"] = True
        agent["skip_download_model_summary"] = False

    result = await run_browser_agent(task, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
