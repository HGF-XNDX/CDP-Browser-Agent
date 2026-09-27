from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from ..configuration import load_config
from ..harness.skills import SkillCatalog
from .runner import run_browser_agent
from ..workflows.runner import run_workflow
from ..workflows.spec import WorkflowCatalog
from ..workflows.store import WorkflowStore


def _load_config(path: str | None) -> dict[str, Any]:
    return load_config(path)


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
    parser.add_argument("--skill-path", action="append", default=[], help="Skill folder or parent directory; repeatable.")
    parser.add_argument("--skill", action="append", default=[], help="Activate a discovered skill by name; repeatable.")
    parser.add_argument("--list-skills", action="store_true", help="List skill metadata without starting browser/model/MCP clients.")
    parser.add_argument("--export-skill", metavar="DIRECTORY", help="Export the bundled cdp-browser-agent skill into a parent directory.")
    parser.add_argument("--run-timeout", type=float, help="Whole run deadline in seconds.")
    parser.add_argument("--workflow", help="Run a configured website workflow by name.")
    parser.add_argument("--workflow-path", action="append", default=[], help="Workflow JSON file or directory; repeatable.")
    parser.add_argument("--workflow-params", help="JSON object of workflow parameters.")
    parser.add_argument("--list-workflows", action="store_true")
    parser.add_argument("--resume-run", help="Resume a workflow run ID with the same frozen definition and parameters.")
    parser.add_argument("--page-budget", type=int, help="Pause after this many collected listing pages in this invocation.")
    parser.add_argument("--retry-uncertain-step", action="store_true", help="Explicitly retry an interrupted agent step after checking its effects.")
    parser.add_argument("--workflow-status", metavar="RUN_ID")
    parser.add_argument("--pause-workflow", metavar="RUN_ID", help="Request cooperative pause at the next checkpoint boundary.")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    if args.export_skill:
        source = Path(__file__).resolve().parents[1] / "skills" / "cdp-browser-agent"
        target = Path(args.export_skill).expanduser().resolve() / source.name
        shutil.copytree(source, target)  # Refuse to overwrite an existing customized skill.
        print(json.dumps({"skill_path": str(target)}, ensure_ascii=False))
        return
    task = args.task_option or args.task
    config = _load_config(args.config)
    workflows = config.setdefault("workflows", {})
    workflows.setdefault("paths", []).extend(str(Path(p).expanduser().resolve()) for p in args.workflow_path)
    if args.list_workflows:
        print(json.dumps(WorkflowCatalog(workflows["paths"]).catalog(), ensure_ascii=False, indent=2))
        return
    if args.workflow_status or args.pause_workflow:
        store = WorkflowStore(workflows.get("state_dir", "workflow-runs"))
        try:
            result = store.request_pause(args.pause_workflow) if args.pause_workflow else store.result(store.get(args.workflow_status))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return
    harness = config.setdefault("harness", {})
    harness.setdefault("skill_paths", []).extend(str(Path(p).expanduser().resolve()) for p in args.skill_path)
    harness.setdefault("active_skills", []).extend(args.skill)
    if args.run_timeout is not None:
        harness["run_timeout_seconds"] = args.run_timeout
    if args.list_skills:
        skills = SkillCatalog(harness["skill_paths"], int(harness.get("max_skill_chars", 20000)), int(harness.get("active_skill_budget_chars", 30000)))
        print(json.dumps(skills.catalog(limit=50), ensure_ascii=False, indent=2))
        return
    if not task and not args.workflow:
        raise SystemExit("error: provide a task as positional text or --task")
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

    if args.workflow:
        params = json.loads(args.workflow_params) if args.workflow_params is not None else None
        result = await run_workflow(args.workflow, config, params, resume_run_id=args.resume_run,
                                    page_budget=args.page_budget, retry_uncertain_step=args.retry_uncertain_step)
    else:
        result = await run_browser_agent(task, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
