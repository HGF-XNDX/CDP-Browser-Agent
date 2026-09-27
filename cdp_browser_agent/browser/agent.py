from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from .controller import BrowserController
from .memory import BrowserAgentMemory, classify_page_from_observation
from .planner import compact_observation, plan_next_action
from .policy import validate_action
from .resource_analyzer import analyze_downloaded_resource, collect_observed_candidate_resources, write_download_manifest
from .site_memory import BrowserSiteMemory, target_element
from .strategy_evaluator import evaluate_strategy
from ..harness.runtime import ExtensionRuntime
from ..harness.session import RunSession


log = logging.getLogger(__name__)


def progress_signature(observation: dict, last_action: dict, last_result: dict) -> str:
    # Includes same-page content and form state; URL/count alone misses SPA progress.
    content = {k: observation.get(k) for k in ("url", "fullText", "visibleText", "scrollY", "elements")}
    if last_action.get("action") == "tool":
        content["tool"] = [last_action, last_result]
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def collect_source(sources: list[dict], observation: dict) -> list[dict]:
    url = observation.get("url", "")
    text = observation.get("fullText") or observation.get("visibleText") or ""
    if not url.startswith(("http://", "https://")) or not text.strip():
        return sources
    source = {"url": url, "title": observation.get("title", ""), "kind": "page", "snippet": text[:6000]}
    return [s for s in sources if s["url"] != url][-19:] + [source]


async def run_agent(task: str, config: dict, runtime: ExtensionRuntime | None = None, *, session: RunSession | None = None,
                    controller: BrowserController | None = None, completion_check=None, action_guard=None) -> dict:
    if runtime is None:
        from .runner import run_browser_agent
        return await run_browser_agent(task, config, completion_check=completion_check)
    settings = config.get("agent", {})
    model_settings = {**config.get("model", {}), "allowPasswordInput": settings.get("allow_password_input", False)}
    owns_session = session is None
    session = session or RunSession(config, task)
    recorder = session.recorder
    state = session.state
    owns_controller = controller is None
    site_memory = None
    memory = BrowserAgentMemory(agent_settings=settings, model_settings=model_settings)
    max_steps = int(settings.get("max_steps", 40))
    seen_progress: dict[str, int] = {}
    no_progress_limit = max(2, int(settings.get("browser_no_progress_hard", 8)))
    previous_action = {}
    model_errors = 0
    feedback = None
    pending_screenshot = None
    try:
        if controller is None:
            controller = await BrowserController.launch(config)
        if settings.get("browser_site_memory_enabled", False):
            site_memory = BrowserSiteMemory(Path(settings.get("memory_dir", "memory")) / "browser_site_memory.json")
        for step in range(1, max_steps + 1):
            state["step"] = step
            observation = await controller.observe()
            state["sources"] = collect_source(state["sources"], observation)
            state["observed_resource_candidates"] = collect_observed_candidate_resources(state["observed_resource_candidates"], observation)
            signature = progress_signature(observation, previous_action, state["last_result"] or {})
            seen_progress[signature] = seen_progress.get(signature, 0) + 1
            if seen_progress[signature] >= no_progress_limit:
                state.update(status="stalled", stopped_reason="no_progress", answer="Stopped after repeated observations without new progress.")
                break
            recorder.write("observe", {"step": step, "observation": compact_observation(observation)})
            extensions = runtime.context()
            base = {"task": task, "observation": compact_observation(observation),
                    "last_result": state["last_result"], "extensions": extensions}
            memory_context = memory.build_context(task, observation, state["last_result"], state["sources"], base)
            if site_memory:
                memory_context["site_memory"] = site_memory.recall(observation.get("url", ""))
            request = {"task": task, "step": step, "observation": observation,
                       "page_context": await controller.page_context(), "model_settings": model_settings,
                       "agent_settings": settings, "memory_context": memory_context,
                       "memory": memory.to_legacy_memory(), "sources": state["sources"],
                       "history": state["history"], "last_result": state["last_result"],
                       "extensions": extensions, "strategy_feedback": feedback, "screenshot": pending_screenshot}
            pending_screenshot = None
            reviews = []
            try:
                action = validate_action((await plan_next_action(request))["action"], observation, request)
                if action_guard:
                    action_guard(action, observation)
                model_errors = 0
            except Exception as exc:
                model_errors += 1
                state["last_result"] = {"ok": False, "errorType": "planner_error", "message": str(exc)[:2000]}
                recorder.write("planner_error", {"step": step, **state["last_result"]})
                if model_errors >= int(settings.get("max_model_errors", 3)):
                    state.update(status="failed", stopped_reason="planner_errors", answer="Planner failed repeatedly; inspect the run log.")
                    break
                continue
            if settings.get("strategy_evaluator_enabled", False) and action["action"] not in {"tool", "ask_user", "observe_vision"}:
                try:
                    feedback = await evaluate_strategy(request, action)
                    reviews.append(memory.record_strategy_evaluation(task, step, action, feedback))
                    recorder.write("strategy_evaluation", {"step": step, "review": feedback})
                    if not feedback["approved"]:
                        state["last_result"] = {"ok": False, "message": feedback["feedback"], "errorType": "strategy_rejected"}
                        continue
                except Exception as exc:
                    recorder.write("evaluator_error", {"step": step, "message": str(exc)})
            recorder.write("action_start", {"step": step, "action": action})
            log.info("run=%s step=%s action=%s", recorder.run_id, step, action["action"])
            if action["action"] == "done":
                if action["outcome"] == "completed" and completion_check:
                    try:
                        verification = await completion_check(controller, state)
                    except Exception as exc:
                        verification = {"ok": False, "error": str(exc)[:1000]}
                    state["verification"] = verification
                    recorder.write("completion_check", {"step": step, "verification": verification})
                    if verification.get("ok") is not True:
                        state["last_result"] = {"ok": False, "errorType": "completion_check_failed", "verification": verification,
                                                "message": "Host completion checks failed. Continue the task or report incomplete/blocked."}
                        continue
                state.update(status=action["outcome"], stopped_reason="done", answer=action["answer"], completion_basis="model_reported")
                if completion_check and action["outcome"] == "completed":
                    state["completion_basis"] = "host_verified"
                if site_memory and state["status"] == "completed":
                    site_memory.record_successful_workflow(task, state["history"])
                break
            if action["action"] == "ask_user":
                state.update(status="needs_input", stopped_reason="ask_user", answer=action["message"])
                break
            try:
                if action["action"] == "tool":
                    result = await runtime.registry.call(action["name"], action["arguments"])
                elif action["action"] == "observe_vision":
                    if model_settings.get("enableVision", False):
                        pending_screenshot = await controller.capture_screenshot_for_vision()
                    result = {"ok": bool(pending_screenshot), "message": "vision_captured" if pending_screenshot else "vision_unavailable"}
                else:
                    result = await controller.execute(action)
            except Exception as exc:
                result = {"ok": False, "errorType": type(exc).__name__, "message": str(exc)[:2000]}
            entry = {"actionId": f"A{step:04d}", "step": step,
                     "phase": "step_success" if result.get("ok") else "step_failed",
                     "url": observation.get("url", ""), "title": observation.get("title", ""),
                     "pageType": classify_page_from_observation(observation),
                     "snippet": (observation.get("visibleText") or "")[:800],
                     "action": action, "targetElement": target_element(observation, action),
                     "result": result, "strategyReviews": reviews, "sourceCount": len(state["sources"])}
            state["history"].append(entry)
            state["last_result"] = result
            previous_action = action
            recorder.write("action_result", {"step": step, "action": action, "result": result})
            await memory.add_entry(entry, task, model_settings)
            if site_memory:
                site_memory.record(entry)
            if action["action"] != "tool" and result.get("ok") and result.get("path"):
                state["collected_files"].append(result["path"])
                # An artifact is evidence of one action, not completion of a whole task.
        else:
            state.update(status="max_steps", stopped_reason="max_steps", answer=f"Reached max steps ({max_steps}); task completion is unverified.")
    except asyncio.CancelledError:
        if state["status"] != "timeout":
            state.update(status="cancelled", stopped_reason="cancelled")
        raise
    except Exception as exc:
        state.update(status="failed", stopped_reason="runtime_error", answer=str(exc)[:2000])
    finally:
        state["memory_stats"] = memory.stats()
        if site_memory:
            try:
                site_memory.save()
            except Exception as exc:
                recorder.write("site_memory_error", {"message": str(exc)})
        if controller and owns_controller:
            try:
                if settings.get("write_download_manifest", True):
                    download_dir, analysis_dir = controller.artifact_directories()
                    write_download_manifest(download_dir, state, analysis_dir)
                if settings.get("analyze_downloads_after_run", False) and state["status"] not in {"cancelled", "timeout"}:
                    download_dir, analysis_dir = controller.artifact_directories()
                    state["download_analysis"] = await analyze_downloaded_resource(config, download_dir,
                        skip_model_summary=bool(settings.get("skip_download_model_summary", True)), analysis_dir=analysis_dir)
            except Exception as exc:
                recorder.write("artifact_error", {"message": str(exc)})
            finally:
                await controller.close()
        if owns_session:
            session.finish()
    return state
