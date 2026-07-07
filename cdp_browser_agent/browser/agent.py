from __future__ import annotations

import asyncio
import html
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .controller import BrowserController
from ..model_client import chat_completion
from .memory import BrowserAgentMemory, classify_page_from_observation
from .planner import compact_observation, compact_sources, plan_next_action, synthesize_final_answer
from .policy import detect_blocking_page, enforce_page_scan, enforce_source_depth, prevent_repeated_action, validate_action
from .resource_analyzer import analyze_downloaded_resource, collect_observed_candidate_resources, write_download_manifest
from .site_memory import BrowserSiteMemory, target_element
from .strategy_evaluator import evaluate_strategy


ACTION_MEMORY_RECENT_LIMIT = 10
ACTION_MEMORY_COMPACT_BATCH = 5

_LAW_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千万零〇\d]+条")


def _file_has_law_articles(path: str | None, min_count: int = 3) -> bool:
    """True if the saved file actually contains law text — at least `min_count`
    DISTINCT 第N条 article markers. Guards against a collection run 'succeeding' on a
    metadata/landing page (e.g. flk.npc.gov.cn detail shell) that has no article body."""
    if not path:
        return False
    try:
        file_path = Path(path)
        if file_path.suffix.lower() == ".docx":
            with zipfile.ZipFile(file_path) as archive:
                xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
            text = html.unescape(re.sub(r"<[^>]+>", "", xml))
        else:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return len(set(_LAW_ARTICLE_RE.findall(text))) >= min_count


def action_signature(action: dict | None) -> str:
    action = action or {}
    comparable = {
        "action": action.get("action", ""),
        "target_id": action.get("target_id", ""),
        "page_id": action.get("page_id", ""),
        "url": action.get("url", ""),
        "text": action.get("text", ""),
        "amount": action.get("amount", ""),
        "filename": action.get("filename", ""),
    }
    return json.dumps(comparable, ensure_ascii=False, sort_keys=True)


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
        if not parsed.scheme or not parsed.netloc:
            return url or ""
        path = parsed.path.rstrip("/") or "/"
        return parsed._replace(path=path, fragment="").geturl()
    except Exception:
        return url or ""


def host(url: str) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except Exception:
        return ""


def is_search_page(url: str) -> bool:
    hostname = host(url).lower()
    return any(domain in hostname for domain in ["baidu.com", "bing.com", "duckduckgo.com", "google.com", "sogou.com", "sm.cn", "so.com"])


def is_low_value_source_url(url: str) -> bool:
    hostname = host(url).lower()
    lowered = (url or "").lower()
    if not hostname:
        return True
    if hostname == "hao123.com" or hostname.endswith(".hao123.com"):
        return True
    if hostname in {"image.baidu.com", "map.baidu.com", "v.baidu.com", "haokan.baidu.com", "tieba.baidu.com"}:
        return True
    return "login" in lowered or "passport" in lowered or "captcha" in lowered


def is_flk_detail_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    return (
        parsed.hostname == "flk.npc.gov.cn"
        and parsed.path.rstrip("/") == "/detail"
        and bool((parse_qs(parsed.query).get("id") or [""])[0])
    )


def flk_filename_from_observation(observation: dict) -> str:
    title = str((observation or {}).get("title") or "").split("-", 1)[0].strip()
    title = re.sub(r"[\\/:*?\"<>|]+", "", title)
    return f"{title or 'flk_law'}.docx"


def summarize_observation(observation: dict) -> dict:
    return {
        "url": observation.get("url", ""),
        "title": observation.get("title", ""),
        "pageType": observation.get("pageType") or classify_page_from_observation(observation),
        "snippet": " ".join((observation.get("visibleText") or "").split())[:800],
    }


def is_error_like_source_content(title: str = "", url: str = "", text: str = "") -> bool:
    haystack = f"{title}\n{url}\n{text}".lower()
    return (
        bool(re.search(r"\b404\b", haystack))
        or "page not found" in haystack
        or "not found" in haystack
        or "browser error" in haystack
    )


def collect_source(sources: list[dict], observation: dict) -> list[dict]:
    text = " ".join((observation.get("visibleText") or "").split())
    if len(text) < 80:
        return sources
    url = normalize_url(observation.get("url", ""))
    if is_low_value_source_url(url):
        return sources
    title = observation.get("title") or ""
    if is_error_like_source_content(title, url, text):
        return sources
    source = {
        "url": url,
        "title": observation.get("title") or url,
        "host": host(url),
        "kind": "search" if is_search_page(url) else "page",
        "snippet": text[:6000],
        "usable": not is_search_page(url) and not is_low_value_source_url(url) and len(text) >= 80,
    }
    for index, existing in enumerate(sources):
        if normalize_url(existing.get("url", "")) == url:
            sources[index] = source
            return sources[-20:]
    sources.append(source)
    return sources[-20:]


def usable_source_count(sources: list[dict]) -> int:
    seen = set()
    count = 0
    for source in sources or []:
        url = normalize_url(source.get("url", ""))
        if source.get("kind") == "search" or is_low_value_source_url(url) or url in seen:
            continue
        if is_error_like_source_content(source.get("title", ""), url, source.get("snippet", "")):
            continue
        if len((source.get("snippet") or "").strip()) < 80:
            continue
        seen.add(url)
        count += 1
    return count


def parse_json_object(text: str) -> dict:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.removeprefix("```json").removeprefix("```").strip()
        value = value.removesuffix("```").strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(value[start : end + 1])


def action_line(action: dict) -> str:
    parts = [f"action={action.get('action')}"]
    if action.get("target_id"):
        parts.append(f"target={action['target_id']}")
    if action.get("url"):
        parts.append(f"url={action['url']}")
    if action.get("reason"):
        parts.append(f"reason={action['reason']}")
    if action.get("message"):
        parts.append(action["message"])
    return " | ".join(parts)


class RunLogger:
    def __init__(self, config: dict, task: str):
        agent_settings = config.get("agent") or {}
        self.console_verbose = bool(agent_settings.get("console_verbose", False))
        shared_events = str(agent_settings.get("shared_events_path") or "").strip()
        self.shared_events_path = Path(shared_events).resolve() if shared_events else None
        self.shared_workflow_id = str(agent_settings.get("shared_workflow_id") or "")
        self.path: Path | None = None
        # When a shared events path is available, all events flow there — skip the per-run file.
        create_separate = bool(agent_settings.get("write_run_log", True)) and not self.shared_events_path
        if create_separate:
            log_dir_value = agent_settings.get("log_dir") or Path(__file__).resolve().parents[2] / "logs"
            log_dir = Path(log_dir_value) if Path(str(log_dir_value)).is_absolute() else Path(__file__).resolve().parents[2] / log_dir_value
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.path = log_dir / f"run_{stamp}.jsonl"
        self.write("run_start", {"task": task})

    def write(self, event: str, payload: dict | None = None) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **(payload or {}),
        }
        if self.path:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.shared_events_path:
            shared_record = {
                "timestamp": record["ts"],
                "workflow_id": self.shared_workflow_id,
                "actor": "cdp_agent",
                "kind": f"cdp_{event}",
                **(payload or {}),
            }
            with self.shared_events_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(shared_record, ensure_ascii=False, default=str) + "\n")


def print_phase0_config_summary(config: dict, model_settings: dict, agent_settings: dict) -> None:
    context_tokens = int(agent_settings.get("context_window_tokens") or 8192)
    output_tokens = int(model_settings.get("maxTokens") or 1024)
    print(
        "config="
        f"context_window_tokens:{context_tokens} "
        f"max_output_tokens:{output_tokens} "
        f"allow_password_input:{bool(agent_settings.get('allow_password_input', False))} "
        f"memory_model_summaries:{bool(agent_settings.get('memory_use_model_summaries', True))}"
    )


async def run_agent(task: str, config: dict) -> dict:
    controller = await BrowserController.launch(config)
    run_logger = RunLogger(config, task)
    if run_logger.path:
        print(f"log_file={run_logger.path}")
    browser_settings = config.get("browser") or {}
    keep_open_after_run = bool(browser_settings.get("keep_open_after_run", False)) and not controller.connected_over_cdp
    max_steps = int((config.get("agent") or {}).get("max_steps", 40))
    agent_settings = config.get("agent") or {}
    manual_poll_ms = int(agent_settings.get("manual_poll_ms", 3000))
    model_error_wait_ms = int(agent_settings.get("model_error_wait_ms", 2000))
    strategy_evaluator_enabled = bool(agent_settings.get("strategy_evaluator_enabled", True))
    strategy_evaluator_rounds = max(0, int(agent_settings.get("strategy_evaluator_rounds", 1)))
    model_first_strategy = bool(agent_settings.get("model_first_strategy", True))
    model_settings = {
        **(config.get("model") or {}),
        "historyCompressAfter": int(agent_settings.get("history_compress_after", 40)),
        "allowPasswordInput": bool(agent_settings.get("allow_password_input", False)),
        "memoryUseModelSummaries": bool(agent_settings.get("memory_use_model_summaries", True)),
    }
    model_settings["enableVision"] = bool(model_settings.get("enableVision", False))

    memory_manager = BrowserAgentMemory(agent_settings=agent_settings, model_settings=model_settings)
    memory_dir_value = agent_settings.get("memory_dir") or "memory"
    memory_dir = Path(str(memory_dir_value))
    if not memory_dir.is_absolute():
        memory_dir = Path(__file__).resolve().parents[2] / memory_dir
    site_memory_enabled = bool(
        agent_settings.get("browser_site_memory_enabled", True)
    )
    site_memory = BrowserSiteMemory(
        memory_dir / "browser_site_memory.json",
        max_steps_per_site=int(agent_settings.get("browser_site_memory_max_steps", 80)),
    )
    print_phase0_config_summary(config, model_settings, agent_settings)
    run_logger.write("site_memory_loaded", site_memory.stats())

    state = {
        "task": task,
        "step": 0,
        "history": [],
        "memory": [],
        "sources": [],
        "last_result": None,
        "answer": "",
        "memory_stats": memory_manager.stats(),
        "observed_resource_candidates": [],
    }

    async def remember_entry(entry: dict) -> None:
        await memory_manager.add_entry(entry, task, model_settings)
        if not site_memory_enabled:
            return
        site_memory.record(entry)
        try:
            site_memory.save()
        except Exception as exc:
            run_logger.write("site_memory_save_failed", {"error": str(exc)})

    def remember_successful_workflow() -> None:
        if not site_memory_enabled:
            return
        site_memory.record_successful_workflow(task, state.get("history") or [])
        try:
            site_memory.save()
            run_logger.write("site_workflow_saved", site_memory.stats())
        except Exception as exc:
            run_logger.write("site_memory_save_failed", {"error": str(exc)})

    async def finalize_download_artifacts(reason: str) -> None:
        download_dir, analysis_dir = controller.artifact_directories()
        download_dir = download_dir.resolve()
        analysis_dir = analysis_dir.resolve()
        try:
            if bool(agent_settings.get("write_download_manifest", True)):
                write_download_manifest(download_dir, state, analysis_dir)
                run_logger.write("download_manifest_written", {"reason": reason, "download_dir": str(download_dir)})
        except Exception as exc:
            run_logger.write("download_manifest_failed", {"reason": reason, "error": str(exc)})
        if not bool(agent_settings.get("analyze_downloads_after_run", False)):
            return
        try:
            analysis = await analyze_downloaded_resource(
                config,
                download_dir,
                skip_model_summary=bool(agent_settings.get("skip_download_model_summary", False)),
                analysis_dir=analysis_dir,
            )
            state["download_analysis"] = analysis
            run_logger.write("download_analysis", {"reason": reason, "analysis": analysis})
        except Exception as exc:
            state["download_analysis"] = {"ok": False, "message": str(exc)}

    # No-progress circuit breaker: detect when the agent makes no meaningful
    # progress (same URL, no new sources/candidates) for several steps. At the
    # soft threshold we inject actionable recovery guidance so the planner can
    # change strategy; at the hard threshold we abort THIS run gracefully with a
    # diagnosis instead of grinding to max_steps in a wait/reject loop.
    no_progress_soft = max(2, int(agent_settings.get("browser_no_progress_soft", 4)))
    no_progress_hard = max(no_progress_soft + 2, int(agent_settings.get("browser_no_progress_hard", 8)))
    _prev_progress_sig = None
    _no_progress = 0

    try:
        while state["step"] < max_steps:
            state["step"] += 1
            observation = await controller.observe()
            state["observed_resource_candidates"] = collect_observed_candidate_resources(
                state.get("observed_resource_candidates", []),
                observation,
            )
            state["sources"] = collect_source(state["sources"], observation)

            # Progress = a change in URL / count of sources / candidate resources.
            _progress_sig = (
                observation.get("url"),
                len(state["sources"]),
                len(state.get("observed_resource_candidates") or []),
            )
            if _progress_sig == _prev_progress_sig:
                _no_progress += 1
            else:
                _no_progress = 0
            _prev_progress_sig = _progress_sig

            if _no_progress >= no_progress_hard:
                # Graceful failure with a diagnosis — do NOT keep erroring.
                reason = (
                    f"无进展熔断：连续 {_no_progress} 步在 {observation.get('url')} 未取得任何进展"
                    f"（URL 未变、无新信息/资源）。已放弃本次采集。可能原因：页面交互无法完成"
                    f"（如搜索未成功提交、动态内容未加载），或目标不在此页面。"
                )
                state["answer"] = reason
                state["stopped_reason"] = "no_progress_circuit_break"
                print(f"[step {state['step']}] CIRCUIT BREAK | {reason}")
                run_logger.write("no_progress_circuit_break", {
                    "step": state["step"], "url": observation.get("url"), "no_progress_steps": _no_progress,
                })
                await finalize_download_artifacts("no_progress_circuit_break")
                break

            scroll = observation.get("scroll") or {}
            print(
                f"\n[step {state['step']}] observe | "
                f"title={observation.get('title')} | "
                f"elements={len(observation.get('elements') or [])} | "
                f"url={observation.get('url')}"
            )
            run_logger.write("observe", {"step": state["step"], "observation": observation, "sources": state["sources"]})

            # Deterministic handoff for 国家法律法规数据库. Once the browser agent
            # has navigated to the correct flk detail page, stop asking the planner
            # to click dynamic download menus and use the site's official download
            # API via BrowserController.download_url(). This produces the real DOCX
            # instead of the 455-byte SPA shell.
            if bool(agent_settings.get("require_law_articles", False)) and is_flk_detail_url(observation.get("url", "")):
                action = {
                    "action": "download",
                    "url": observation.get("url", ""),
                    "filename": flk_filename_from_observation(observation),
                    "resource_name": controller.active_resource_name,
                    "reason": "flk_detail_auto_download",
                }
                run_logger.write("validated_action", {"step": state["step"], "action": action})
                print(f"[step {state['step']}] action=download | url={action['url']} | reason=flk_detail_auto_download")
                try:
                    result = await controller.download_url(
                        action["url"], action.get("filename"), action.get("resource_name")
                    )
                except Exception as exc:
                    result = {"ok": False, "message": str(exc), "errorType": "flk_auto_download_error"}
                print(f"[step {state['step']}] result={result.get('message')} ok={result.get('ok')}")
                run_logger.write("action_result", {"step": state["step"], "action": action, "result": result})

                summary = summarize_observation(observation)
                entry = {
                    "actionId": f"A{state['step']:04d}",
                    "step": state["step"],
                    "phase": "step_success" if result.get("ok") else "step_failed",
                    "url": summary["url"],
                    "title": summary["title"],
                    "pageType": summary["pageType"],
                    "snippet": summary["snippet"],
                    "action": action,
                    "targetElement": target_element(observation, action),
                    "result": result,
                    "strategyReviews": [],
                    "sourceCount": len(state["sources"]),
                    "usableSourceCount": usable_source_count(state["sources"]),
                }
                state["history"].append(entry)
                await remember_entry(entry)
                state["memory"] = memory_manager.to_legacy_memory()
                state["memory_stats"] = memory_manager.stats()
                state["last_result"] = result

                if result.get("ok") and result.get("path") and _file_has_law_articles(result.get("path")):
                    state.setdefault("collected_files", []).append(result.get("path"))
                    state["answer"] = f"已保存文件：{result.get('path')}（来源：{result.get('url') or action['url']}）"
                    state["stopped_reason"] = "collection_complete"
                    run_logger.write("collection_complete", {
                        "step": state["step"], "path": result.get("path"), "url": result.get("url") or action["url"],
                    })
                    print(f"[step {state['step']}] COLLECTION COMPLETE | saved {result.get('path')}")
                    remember_successful_workflow()
                    await finalize_download_artifacts("collection_complete")
                    return state

                state["last_result"] = {
                    "ok": False,
                    "message": (
                        "flk 详情页自动下载未得到可用法条 DOCX；请改用页面下载菜单或其他全文来源。"
                    ),
                }
                run_logger.write("flk_auto_download_rejected", {
                    "step": state["step"], "path": result.get("path"), "url": result.get("url") or action["url"],
                })

            base_payload_for_budget = {
                "task": task,
                "step": state["step"],
                "last_result": state.get("last_result"),
                "sources": compact_sources(state.get("sources", [])),
                "observation": compact_observation(observation),
            }
            page_context = await controller.page_context()
            memory_context = memory_manager.build_context(
                task=task,
                observation=observation,
                last_result=state.get("last_result"),
                sources=state.get("sources", []),
                base_payload=base_payload_for_budget,
            )
            memory_context["site_memory"] = (
                site_memory.recall(observation.get("url", ""))
                if site_memory_enabled
                else {}
            )
            memory_context["site_memory_stats"] = site_memory.stats()
            state["memory_stats"] = memory_manager.stats()
            state["memory_context"] = memory_context
            state["memory"] = memory_manager.to_legacy_memory()

            request = {
                "task": task,
                "step": state["step"],
                "observation": observation,
                "page_context": page_context,
                "screenshot": None,
                "history": state["history"],
                "memory": state["memory"],
                "memory_context": memory_context,
                "sources": state["sources"],
                "last_result": state["last_result"],
                "model_settings": model_settings,
                "agent_settings": agent_settings,
            }
            # Soft threshold: feed the planner actionable recovery guidance (via the
            # strategy_feedback channel it already reads) so it changes strategy
            # instead of repeating the stuck action.
            if _no_progress >= no_progress_soft:
                request["strategy_feedback"] = {
                    "approved": False,
                    "level": "should",
                    "feedback": (
                        f"你已连续 {_no_progress} 步无进展（同一 URL、无新信息）。停止重复当前动作，"
                        f"采取实质不同的策略：① 若搜索框已填好，按 Enter 或点击搜索按钮提交查询；"
                        f"② 尝试直接 navigate 到目标资源/详情页 URL；③ 若本页确实无法取得目标，"
                        f"立即用 finish 结束并明确说明无法获取的原因，不要继续等待或翻页。"
                    ),
                    "suggested_action": "submit_or_navigate_or_finish",
                    "no_progress_steps": _no_progress,
                }
                run_logger.write("no_progress_recovery_hint", {"step": state["step"], "no_progress_steps": _no_progress})
            step_strategy_reviews = []

            action = None if model_first_strategy else detect_blocking_page(request)
            if not action:
                try:
                    planned = await plan_next_action(request)
                    action = planned["action"]
                    run_logger.write("planned_action", {"step": state["step"], "action": action})
                except Exception as exc:
                    print(f"  planner_error={exc}")
                    run_logger.write("planner_error", {"step": state["step"], "error": str(exc)})
                    action = {"action": "wait", "ms": model_error_wait_ms, "reason": "model_error_recovery"}

                if strategy_evaluator_enabled and action.get("action") not in {"ask_user", "observe_vision"}:
                    forbidden_action_signatures: set[str] = set()
                    for evaluator_round in range(strategy_evaluator_rounds + 1):
                        try:
                            evaluation = await evaluate_strategy(request, action)
                            review = memory_manager.record_strategy_evaluation(task, state["step"], action, evaluation)
                            step_strategy_reviews.append(review)
                            run_logger.write("strategy_evaluation", {"step": state["step"], "action": action, "evaluation": evaluation})
                        except Exception as exc:
                            run_logger.write("strategy_evaluation_error", {"step": state["step"], "error": str(exc)})
                            break
                        if evaluation.get("approved", True):
                            break
                        rejected_signature = action_signature(action)
                        repeated_forbidden_action = rejected_signature in forbidden_action_signatures
                        forbidden_action_signatures.add(rejected_signature)
                        feedback_text = evaluation.get("feedback", "")
                        if repeated_forbidden_action:
                            feedback_text = "The same forbidden action was already rejected. Choose a materially different action. " + feedback_text
                        feedback = {
                            "approved": False,
                            "level": evaluation.get("level", "should"),
                            "feedback": feedback_text,
                            "suggested_action": evaluation.get("suggested_action", ""),
                            "rejected_action": action,
                        }
                        print(f"  strategy_feedback={feedback['feedback']}")
                        if evaluator_round >= strategy_evaluator_rounds:
                            action = {"action": "wait", "ms": 500, "reason": "strategy_feedback_unresolved"}
                            break
                        retry_request = {**request, "strategy_feedback": feedback}
                        try:
                            planned = await plan_next_action(retry_request)
                            action = planned["action"]
                        except Exception as exc:
                            run_logger.write("planner_error_after_strategy_feedback", {"step": state["step"], "error": str(exc)})
                            break

                if action.get("action") == "observe_vision":
                    if model_settings.get("enableVision"):
                        screenshot = await controller.capture_screenshot_for_vision()
                        if screenshot:
                            request["screenshot"] = screenshot
                            request["last_result"] = {"ok": True, "message": "vision_screenshot_captured"}
                            try:
                                planned = await plan_next_action(request)
                                action = planned["action"]
                            except Exception as exc:
                                action = {"action": "wait", "ms": model_error_wait_ms, "reason": "model_error_recovery"}
                            if action.get("action") == "observe_vision":
                                action = {"action": "wait", "ms": 500, "reason": "vision_already_provided"}
                        else:
                            action = {"action": "wait", "ms": 500, "reason": "vision_capture_failed"}
                    else:
                        action = {"action": "wait", "ms": 500, "reason": "vision_disabled"}

            if not model_first_strategy:
                action = prevent_repeated_action(action, request)
            action = validate_action(action, observation, request)
            if not model_first_strategy:
                action = enforce_page_scan(action, request)
                action = validate_action(action, observation, request)
                action = enforce_source_depth(action, request)
                action = validate_action(action, observation, request)

            print(f"[step {state['step']}] {action_line(action)}")
            run_logger.write("validated_action", {"step": state["step"], "action": action})

            if action.get("action") == "done":
                state["answer"] = action.get("answer") or "Done."
                run_logger.write("done", {"step": state["step"], "answer": state["answer"]})
                print("\nFinal Answer:\n")
                print(state["answer"])
                remember_successful_workflow()
                await finalize_download_artifacts("done")
                return state

            if action.get("action") == "ask_user":
                message = action.get("message") or "User input required."
                print(f"\nManual intervention requested: {message}")
                run_logger.write("manual_intervention", {"step": state["step"], "message": message})
                summary = summarize_observation(observation)
                entry = {
                    "actionId": f"A{state['step']:04d}",
                    "step": state["step"],
                    "phase": "manual_intervention_poll",
                    "url": summary["url"],
                    "title": summary["title"],
                    "pageType": summary["pageType"],
                    "snippet": summary["snippet"],
                    "action": action,
                    "targetElement": target_element(observation, action),
                    "result": {"ok": True, "message": "manual_intervention_poll"},
                    "strategyReviews": step_strategy_reviews,
                    "sourceCount": len(state["sources"]),
                    "usableSourceCount": usable_source_count(state["sources"]),
                }
                state["history"].append(entry)
                await remember_entry(entry)
                state["memory"] = memory_manager.to_legacy_memory()
                state["last_result"] = {"ok": True, "message": "manual_intervention_poll"}
                await asyncio.sleep(max(manual_poll_ms, 500) / 1000)
                continue

            try:
                result = await controller.execute(action)
            except Exception as exc:
                result = {"ok": False, "message": str(exc), "errorType": "action_error"}

            print(f"[step {state['step']}] result={result.get('message')} ok={result.get('ok')}")
            run_logger.write("action_result", {"step": state["step"], "action": action, "result": result})

            summary = summarize_observation(observation)
            action_id = f"A{state['step']:04d}"
            entry = {
                "actionId": action_id,
                "step": state["step"],
                "phase": "step_success",
                "url": summary["url"],
                "title": summary["title"],
                "pageType": summary["pageType"],
                "snippet": summary["snippet"],
                "action": action,
                "targetElement": target_element(observation, action),
                "result": result,
                "strategyReviews": step_strategy_reviews,
                "sourceCount": len(state["sources"]),
                "usableSourceCount": usable_source_count(state["sources"]),
            }
            state["history"].append(entry)
            await remember_entry(entry)
            state["memory"] = memory_manager.to_legacy_memory()
            state["memory_stats"] = memory_manager.stats()
            state["last_result"] = result

            # Collection convergence: one browser run targets ONE resource
            # (see _browser_collection_task — it asks for the resource's full
            # authoritative text, not per-article re-search). A download/save_page
            # that actually produced a file satisfies this run, so finish now.
            # Otherwise the planner keeps re-proposing the same download, the
            # strategy evaluator forbids each as redundant, and every idle step
            # burns a planner+evaluator LLM round-trip until max_steps — pure waste.
            if action.get("action") in {"download", "save_page"} and result.get("ok") and result.get("path"):
                # For LAW collection, the saved file must actually contain 法条正文
                # (multiple 第N条). Otherwise the agent "converged" on a detail/landing
                # page (e.g. flk.npc.gov.cn shell: 说明/纠错/关联推荐, no article text).
                # Don't finish — tell the planner to open the real full-text page.
                if bool(agent_settings.get("require_law_articles", False)) and not _file_has_law_articles(result.get("path")):
                    result = {
                        **result, "ok": False, "message": "saved_page_no_law_articles",
                    }
                    state["last_result"] = {
                        "ok": False,
                        "message": (
                            "保存的页面没有法条正文（找不到多条『第N条』），疑似法规详情页/导航页而非全文。"
                            "请进入该法律的全文页再 save_page：①点击页面上『全文/公报原版/正文』等链接；"
                            "②或换一个有完整条文的来源（优先维基文库 zh.wikisource.org 搜该法名）。不要在此页重复保存。"
                        ),
                    }
                    run_logger.write("collection_rejected_no_articles", {
                        "step": state["step"], "path": result.get("path"), "url": result.get("url"),
                    })
                    print(f"[step {state['step']}] saved page has NO 法条正文 — keep searching for full text")
                else:
                    collected = state.setdefault("collected_files", [])
                    collected.append(result.get("path"))
                    state["answer"] = f"已保存文件：{result.get('path')}（来源：{result.get('url') or ''}）"
                    state["stopped_reason"] = "collection_complete"
                    run_logger.write("collection_complete", {
                        "step": state["step"], "path": result.get("path"), "url": result.get("url"),
                    })
                    print(f"[step {state['step']}] COLLECTION COMPLETE | saved {result.get('path')}")
                    remember_successful_workflow()
                    await finalize_download_artifacts("collection_complete")
                    return state

            await asyncio.sleep(0.5)

        if state["sources"]:
            state["answer"] = await synthesize_final_answer(task, state["sources"], model_settings)
            run_logger.write("synthesized_final_answer", {"answer": state["answer"]})
            print("\nFinal Answer synthesized from captured sources:\n")
            print(state["answer"])
            remember_successful_workflow()
            await finalize_download_artifacts("synthesized_final_answer")
            return state
        state["answer"] = f"Reached max steps ({max_steps}) without final answer."
        run_logger.write("max_steps", {"answer": state["answer"]})
        print(state["answer"])
        await finalize_download_artifacts("max_steps")
        return state
    finally:
        if site_memory_enabled:
            try:
                site_memory.save()
            except Exception as exc:
                run_logger.write("site_memory_save_failed", {"error": str(exc)})
        if keep_open_after_run:
            print("\nBrowser is kept open for inspection. Press Enter to close it.")
            try:
                await asyncio.to_thread(input)
            except (EOFError, KeyboardInterrupt):
                pass
        await controller.close()
