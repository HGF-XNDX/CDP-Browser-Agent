from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from ..configuration import load_config
from ..harness.skills import SkillCatalog
from .runner import run_browser_agent
from ..workflows.runner import run_workflow
from ..workflows.spec import WorkflowCatalog
from ..workflows.store import WorkflowStore
from ..web.tools import WebTools


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
    quick = parser.add_mutually_exclusive_group()
    quick.add_argument("--web-search", metavar="QUERY", help="Search directly without calling the model or starting a browser.")
    quick.add_argument("--web-fetch", metavar="URL", help="Read a public page directly without calling the model or starting a browser.")
    quick.add_argument("--crawl-spec", metavar="JSON", help="Run an HTTP crawl from a JSON specification, without a model/browser.")
    quick.add_argument("--crawl-resume", metavar="ID", help="Resume a crawl's durable frontier.")
    quick.add_argument("--crawl-status", metavar="ID")
    quick.add_argument("--crawl-read", metavar="ID", help="Read dataset JSON by character offsets.")
    quick.add_argument("--crawl-pause", metavar="ID")
    parser.add_argument("--crawl-page-budget", type=int, default=10)
    parser.add_argument("--crawl-offset", type=int, default=0)
    parser.add_argument("--crawl-limit", type=int, default=4000)
    parser.add_argument("--web-fetch-format", choices=["text", "html"], default="text")
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
    parser.add_argument("--resume-task", help="Resume a saved ordinary agent task ID.")
    parser.add_argument("--user-input", help="Actual user reply for a resumed task or --task-respond.")
    parser.add_argument("--task-status", nargs="?", const="all", help="Inspect a task or list recent tasks.")
    parser.add_argument("--task-respond", help="Reply to a live task ID waiting for input.")
    parser.add_argument("--request-id", help="Pending input request ID from task status.")
    parser.add_argument("--intervention", choices=["auto", "wait", "return"], help="Human input policy for this task.")
    parser.add_argument("--wait-seconds", type=float, help="Wait time before autonomous decision.")
    parser.add_argument("--processing-path", action="append", default=[], help="Registered processing profile JSON or directory.")
    parser.add_argument("--process-profile", help="Run a data-processing profile directly.")
    parser.add_argument("--processing-input", help="JSON array of {data, source_url} records to process.")
    parser.add_argument("--worker-start", metavar="PROFILE", help="Start a persistent processing conversation with --processing-input.")
    parser.add_argument("--worker-continue", metavar="ID", help="Resume a pending turn or continue with feedback.")
    parser.add_argument("--worker-feedback", help="Follow-up processing instruction, within the frozen method.")
    parser.add_argument("--worker-turn", type=int, help="Last observed turn; required when sending new feedback.")
    parser.add_argument("--worker-status", nargs="?", const="all")
    parser.add_argument("--worker-cancel", metavar="ID")
    parser.add_argument("--experience-list", nargs="?", const="all")
    parser.add_argument("--experience-replay", metavar="ID")
    parser.add_argument("--replay-suite", help="Operator-registered held-out replay suite name.")
    parser.add_argument("--experience-revoke", metavar="ID")
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
    if any((args.crawl_spec, args.crawl_resume, args.crawl_status, args.crawl_read, args.crawl_pause)):
        if task or args.workflow or args.resume_task or args.worker_start or args.process_profile:
            raise SystemExit("Direct crawl operations cannot be combined with tasks, workflows or processing")
        from ..crawler.engine import Crawler
        service = Crawler(config)
        if args.crawl_spec or args.crawl_resume:
            spec = json.loads(Path(args.crawl_spec).read_text(encoding="utf-8-sig")) if args.crawl_spec else None
            result = await service.run(spec=spec, crawl_id=args.crawl_resume, page_budget=args.crawl_page_budget)
        elif args.crawl_status:
            result = service.status(args.crawl_status)
        elif args.crawl_read:
            result = service.read(args.crawl_read, args.crawl_offset, args.crawl_limit)
        else:
            result = service.pause(args.crawl_pause)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config.setdefault("processing", {}).setdefault("paths", []).extend(str(Path(p).resolve()) for p in args.processing_path)
    if args.intervention:
        config.setdefault("intervention", {})["mode"] = args.intervention
    if args.wait_seconds is not None:
        config.setdefault("intervention", {})["wait_seconds"] = args.wait_seconds
    if args.task_status or args.task_respond:
        from ..harness.task_store import TaskStore
        store = TaskStore(config)
        try:
            result = store.respond(args.task_respond, args.request_id, args.user_input) if args.task_respond else (
                store.list() if args.task_status == "all" else store.get(args.task_status))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return
    if args.web_search is not None or args.web_fetch is not None:
        if task or args.workflow:
            raise SystemExit("Direct web tools cannot be combined with a task or workflow")
        web = WebTools(config.get("web", {}))
        result = await web.search(args.web_search) if args.web_search is not None else await web.fetch(args.web_fetch, content_format=args.web_fetch_format)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
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
    session_commands = [args.worker_start, args.worker_continue, args.worker_status, args.worker_cancel,
                        args.experience_list, args.experience_replay, args.experience_revoke]
    if sum(bool(v) for v in session_commands) > 1:
        raise SystemExit("Choose one worker/experience operation")
    if any(session_commands) and (task or args.workflow or args.resume_task or args.process_profile):
        raise SystemExit("Worker/experience operations cannot be combined with tasks or one-shot processing")
    if (args.worker_feedback is not None or args.worker_turn is not None) and not args.worker_continue:
        raise SystemExit("--worker-feedback and --worker-turn require --worker-continue")
    if not task and not args.workflow and not args.resume_task and not args.process_profile and not any(session_commands):
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

    if any(session_commands):
        from ..processing.sessions import ProcessingSessions, WorkerBusy
        from ..processing.learning import ProcedureStore, ReplayCatalog, replay_experience
        service = ProcessingSessions(config)
        if args.worker_start:
            if not args.processing_input:
                raise SystemExit("--processing-input is required")
            records = json.loads(Path(args.processing_input).read_text(encoding="utf-8-sig"))
            created = await service.create(args.worker_start, records)
            try:
                result = await service.run(created["worker_session_id"])
            except WorkerBusy as exc:
                result = {**created, "status": "busy", "message": str(exc)}
        elif args.worker_continue:
            try:
                result = await service.run(args.worker_continue, feedback=args.worker_feedback, expected_turn=args.worker_turn)
            except WorkerBusy as exc:
                result = {"ok": False, "status": "busy", "worker_session_id": args.worker_continue, "message": str(exc)}
        elif args.worker_status:
            result = {"workers": service.list()} if args.worker_status == "all" else service.status(args.worker_status)
        elif args.worker_cancel:
            result = service.cancel(args.worker_cancel)
        elif args.experience_replay:
            if not args.replay_suite:
                raise SystemExit("--replay-suite is required")
            result = await replay_experience(config, args.experience_replay, args.replay_suite)
        else:
            with ProcedureStore(config) as store:
                result = store.revoke(args.experience_revoke) if args.experience_revoke else {
                    "experiences": store.list(None if args.experience_list == "all" else args.experience_list),
                    "replay_suites": ReplayCatalog(config).catalog()}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.process_profile:
        from ..processing.engine import ProcessingEngine
        from uuid import uuid4
        if not args.processing_input:
            raise SystemExit("--processing-input is required")
        records = json.loads(Path(args.processing_input).read_text(encoding="utf-8-sig"))
        result = await ProcessingEngine(config).run(args.process_profile, records,
            Path(config["processing"]["artifact_dir"]) / uuid4().hex)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.workflow:
        params = json.loads(args.workflow_params) if args.workflow_params is not None else None
        result = await run_workflow(args.workflow, config, params, resume_run_id=args.resume_run,
                                    page_budget=args.page_budget, retry_uncertain_step=args.retry_uncertain_step)
    else:
        result = await run_browser_agent(task, config, resume_run_id=args.resume_task, user_input=args.user_input)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
