from __future__ import annotations

import json
import re

from ..model_client import chat_completion
from .planner import compact_observation, compact_page_context


EVALUATOR_SYSTEM_PROMPT = """You are a strategy evaluator for CDPAgent.

Your job is not to choose the next browser action. Your job is to review one proposed action against soft strategy preferences and return JSON feedback. Preserve planner autonomy: advise on better alternatives, but only veto an action when executing it would be unsupported, contradicted, destructive to progress, or falsely claim completion.

Return exactly one JSON object:
{
  "level": "suggest"|"should"|"forbid",
  "approved": true|false,
  "severity": "ok"|"minor"|"major",
  "feedback": "short actionable feedback for the planner",
  "suggested_action": "what the planner should try, if any",
  "allowed_actions": ["action styles that are acceptable now"],
  "forbidden_actions": ["action styles or concrete actions that must not be executed now"],
  "preferred_action_style": "click/type/press/scroll/navigate/open_tab/switch_tab/download/save_page/done/none"
}

Feedback levels:
- suggest: the proposed action is allowed; provide an optional better direction without blocking execution.
- should: the proposed action is allowed but materially weaker than an evidenced alternative; record an important recommendation without blocking execution.
- forbid: the proposed action must not execute because it contradicts observed/history evidence, repeats a demonstrated failed path without justification, or relies on an unsupported guessed target.

Approval rule:
- Return approved=true for suggest and should. These are guidance levels, not execution vetoes.
- Return approved=false only for forbid. Use forbid sparingly and only when the proposed action itself must not execute.

Soft strategy preferences:
1. Prefer real page interaction over direct URL editing.
2. Direct navigate/open_tab is acceptable for initial known sites or explicit user-provided URLs.
3. Direct navigate/open_tab has lower priority than using ready visible page controls.
4. On searchable/catalog sites, prefer visible site search, catalog tabs, result lists before hand-editing URLs.
5. Typing keywords into a visible search field and pressing Enter is good page interaction.
6. If the planner already typed into a visible search box, the next action should usually be press Enter or click search.
7. For dataset/resource download tasks, the agent should judge which visible files are real data.
8. Use page_context to reason about open pages.
9. Do not over-constrain diverse tasks.
10. If a type action puts a long natural-language task into a search field, return should and suggest a shorter query.
11. For a proposed done action in a download/save task, review completion against acquired artifacts.
12. Do not evaluate safety-critical policy.
13. The planner owns business judgment.

Be concise. If rejecting, explain what the planner should try next.
"""


def extract_json_object(text: str) -> dict:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", value)
        if not match:
            raise
        return json.loads(match.group(0))


def compact_action(action: dict) -> dict:
    return {
        "page_summary": (action.get("page_summary") or "")[:1200],
        "action": action.get("action", ""),
        "target_id": action.get("target_id", ""),
        "text": (action.get("text") or "")[:300],
        "url": (action.get("url") or "")[:700],
        "filename": (action.get("filename") or "")[:200],
        "reason": (action.get("reason") or "")[:300],
        "answer": (action.get("answer") or "")[:1600],
    }


def compact_memory_list(records: list[dict] | None, limit: int, summary_limit: int = 700) -> list[dict]:
    result = []
    for record in (records or [])[-limit:]:
        result.append({
            "action_id": record.get("action_id") or record.get("actionId") or "",
            "step": record.get("step", ""),
            "url": (record.get("url") or "")[:500],
            "action": record.get("action", ""),
            "success": record.get("success", ""),
            "summary": (record.get("summary") or "")[:summary_limit],
            "outcome": record.get("outcome", ""),
            "lesson": (record.get("lesson") or "")[:360],
        })
    return result


async def evaluate_strategy(request: dict, action: dict) -> dict:
    agent_settings = request.get("agent_settings") or {}
    recent_limit = max(4, int(agent_settings.get("strategy_evaluator_recent_history", 20)))
    recall_limit = max(2, int(agent_settings.get("strategy_evaluator_recalled_history", 10)))
    summary_limit = max(0, int(agent_settings.get("strategy_evaluator_summary_chunks", 8)))
    memory_context = request.get("memory_context") or {}
    model_settings = {
        **(request.get("model_settings") or {}),
        "maxTokens": min(int((request.get("model_settings") or {}).get("maxTokens") or 1024), 900),
        "temperature": 0,
        "enableThinking": False,
    }
    payload = {
        "task": request.get("task", ""),
        "step": request.get("step", 0),
        "last_result": request.get("last_result"),
        "page_context": compact_page_context(request.get("page_context")),
        "proposed_action": compact_action(action),
        "observation": compact_observation(request.get("observation") or {}),
        "task_memory": memory_context.get("task_memory", {}),
        "run_memory_brief": memory_context.get("run_memory_brief", {}),
        "site_memory": memory_context.get("site_memory", {}),
        "recent_history": compact_memory_list(memory_context.get("recent_exact_history"), recent_limit, 850),
        "recalled_relevant_history": compact_memory_list(memory_context.get("recalled_relevant_history"), recall_limit, 850),
        "compressed_action_memory": compact_memory_list(memory_context.get("compressed_action_memory"), summary_limit, 1000),
        "planner_instruction": agent_settings.get(
            "browser_strategy_evaluator_instruction", ""
        ),
    }
    raw = await chat_completion(
        [
            {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model_settings,
    )
    parsed = extract_json_object(raw)
    raw_level = str(parsed.get("level") or "").strip().lower()
    if raw_level in {"forbid", "forbidden", "prohibit", "ban"}:
        level = "forbid"
    elif raw_level in {"should", "must"}:
        level = "should"
    elif parsed.get("approved") is False:
        level = "forbid" if parsed.get("severity") == "major" else "should"
    else:
        level = "suggest"
    return {
        "level": level,
        "approved": level != "forbid",
        "severity": parsed.get("severity") or ("major" if level == "forbid" else "minor" if level == "should" else "ok"),
        "feedback": parsed.get("feedback") or "",
        "suggested_action": parsed.get("suggested_action") or "",
        "allowed_actions": parsed.get("allowed_actions") if isinstance(parsed.get("allowed_actions"), list) else [],
        "forbidden_actions": parsed.get("forbidden_actions") if isinstance(parsed.get("forbidden_actions"), list) else [],
        "preferred_action_style": parsed.get("preferred_action_style") or "none",
    }
