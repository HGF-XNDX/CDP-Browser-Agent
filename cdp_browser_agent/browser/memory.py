from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..model_client import chat_completion, RUN_METRICS
from ..context_budget import ContextBudget, estimate_tokens


DEFAULT_CHARS_PER_TOKEN = 3.0
RESOURCE_FILE_SUFFIXES = (
    ".csv",
    ".json",
    ".jsonl",
    ".tsv",
    ".parquet",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".gz",
    ".pdf",
)


def compact_text(text: object, limit: int = 900) -> str:
    return " ".join(str(text or "").split())[:limit]


def json_preview(value: object, limit: int = 1800) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)[:limit]


SENSITIVE_FIELD_WORDS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "cookie",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "credential",
    "验证码",
    "密码",
    "令牌",
    "密钥",
}


def looks_sensitive_field(value: object) -> bool:
    text = str(value or "").lower()
    return any(word in text for word in SENSITIVE_FIELD_WORDS)


def redact_action_payload(action: dict | None) -> dict:
    action = dict(action or {})
    text = action.get("text")
    target_bits = " ".join(
        str(action.get(key) or "")
        for key in ("target_id", "field_type", "type", "name", "label", "placeholder", "ariaLabel", "selector")
    )
    is_sensitive = bool(action.get("allow_password_input")) or looks_sensitive_field(target_bits)
    if action.get("action") == "type" and isinstance(text, str):
        if is_sensitive:
            action["text"] = "[REDACTED]"
            action["textLength"] = len(text)
            action["redacted"] = True
        elif looks_sensitive_field(text):
            action["text"] = "[REDACTED]"
            action["textLength"] = len(text)
            action["redacted"] = True
    for key in list(action.keys()):
        if looks_sensitive_field(key) and isinstance(action.get(key), str):
            action[key] = "[REDACTED]"
            action["redacted"] = True
    def redact_nested(value):
        if isinstance(value, dict):
            return {k: '[REDACTED]' if looks_sensitive_field(k) else redact_nested(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact_nested(v) for v in value]
        return value
    if isinstance(action.get('arguments'), dict):
        action['arguments'] = redact_nested(action['arguments'])
    return action


def bounded_result_observation(value: object, action_id: str) -> dict:
    """Retain observed fields, not just a prefix filled by transport metadata."""
    for text_limit, item_limit, key_limit, depth_limit in (
        (240, 3, 24, 5), (160, 2, 20, 5), (80, 2, 12, 4), (40, 1, 8, 3)
    ):
        truncated, visited = False, 0

        def project(item, depth=0):
            nonlocal truncated, visited
            visited += 1
            if depth > depth_limit or visited > 256:
                truncated = True
                return '[view omitted; retrieve full result]'
            if isinstance(item, str):
                if len(item) > text_limit:
                    truncated = True
                    return item[:text_limit] + '…'
                return item
            if isinstance(item, dict):
                selected = list(item.items())[:key_limit]
                result = {}
                for key, child in selected:
                    if looks_sensitive_field(key):
                        result[str(key)] = '[REDACTED]'
                    else:
                        result[str(key)] = project(child, depth + 1)
                if len(item) > key_limit:
                    truncated = True
                    result['_view_omitted_fields'] = len(item) - key_limit
                return result
            if isinstance(item, (list, tuple)):
                result = [project(child, depth + 1) for child in item[:item_limit]]
                if len(item) > item_limit:
                    truncated = True
                    result.append({'_view_omitted_items': len(item) - item_limit})
                return result
            if item is None or isinstance(item, (int, float, bool)):
                return item
            return project(str(item), depth)

        view = project(value)
        if len(json.dumps(view, ensure_ascii=False)) <= 3800:
            break
    else:
        view = {'message': 'Structured result exceeds the memory view budget.'}
        truncated = True
    return {'view': view, 'truncated': truncated,
            'full_result': {'tool': 'history_read', 'arguments': {'action_id': action_id}}}


def parse_json_object(text: str) -> dict:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(value[start : end + 1])


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


def path_prefix(url: str) -> str:
    try:
        parsed = urlparse(url or "")
        parts = [part for part in parsed.path.split("/") if part][:2]
        return "/" + "/".join(parts) if parts else "/"
    except Exception:
        return ""


def tokenize(text: object) -> list[str]:
    raw = str(text or "").lower()
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9_@.\-]{2,}|[\u4e00-\u9fff]{2,}", raw, flags=re.I):
        token = token.strip("._-")
        if not token:
            continue
        terms.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", token):
            # Long Chinese strings often have no spaces. Add overlapping short shards
            # so relevance recall works for phrases such as 登录按钮 / 订单页面.
            for size in (2, 3):
                terms.extend(token[index : index + size] for index in range(0, max(len(token) - size + 1, 0)))
    stopwords = {
        "http",
        "https",
        "www",
        "com",
        "html",
        "page",
        "click",
        "type",
        "the",
        "and",
        "with",
        "from",
        "this",
        "that",
        "button",
        "input",
        "页面",
        "当前",
        "点击",
        "操作",
        "按钮",
    }
    return [term for term in terms if term not in stopwords and len(term) >= 2]


PAGE_TYPE_VALUES = {
    "generic_page",
    "search_home",
    "search_results",
    "login",
    "dashboard",
    "cart_or_order",
    "dataset_or_resource",
    "detail",
    "form",
    "error_or_blocked",
}


def _element_text(elements: list[dict] | None, limit: int = 5000) -> str:
    parts: list[str] = []
    for element in (elements or [])[:120]:
        parts.extend(
            str(element.get(key) or "")
            for key in (
                "id",
                "tag",
                "role",
                "type",
                "text",
                "placeholder",
                "ariaLabel",
                "name",
                "title",
                "label",
                "description",
                "nearbyText",
                "containerText",
                "formContext",
                "href",
                "state",
            )
        )
    return compact_text(" ".join(parts), limit)


def _debug_summary_page_type(debug_summary: str) -> str:
    text = debug_summary or ""
    match = re.search(r"(?:page_type|page type)\s*[:=]\s*([a-z_]+)", text, flags=re.I)
    if match:
        value = match.group(1).lower()
        if value in PAGE_TYPE_VALUES:
            return value
    lowered = text.lower()
    if "structure=poor" in lowered and any(word in lowered for word in ["captcha", "blocked", "验证", "访问受限"]):
        return "error_or_blocked"
    return ""


def infer_page_type(
    url: str,
    title: str = "",
    text: str = "",
    elements: list[dict] | None = None,
    debug_summary: str = "",
) -> str:
    """Infer a coarse page type using model classification first, then light fallback.

    This label is for memory/recall context, not for hard policy control.
    Login/account words often appear in ordinary navigation bars, so the fallback
    only classifies login when there is strong URL/form evidence.
    """
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    url_text = f"{hostname} {path} {query}"
    visible = compact_text(f"{title} {text[:2500]}", 3500).lower()
    element_blob = _element_text(elements).lower()
    all_text = f"{url_text} {visible} {element_blob}"
    debug_type = _debug_summary_page_type(debug_summary)
    if debug_type:
        return debug_type

    search_hosts = ("baidu.com", "bing.com", "duckduckgo.com", "google.", "sogou.com", "sm.cn", "so.com")
    if any(host in hostname for host in search_hosts):
        if any(marker in path for marker in ("/s", "/search")) or any(param in query for param in ("q=", "wd=", "query=")):
            return "search_results"
        if any(word in element_blob or word in visible for word in ("search input", "搜索", "百度一下", "search")):
            return "search_home"

    if any(word in all_text for word in ("captcha", "access denied", "blocked", "verify you are human", "人机验证", "验证码", "访问受限", "安全验证")):
        return "error_or_blocked"

    has_password_field = "password input" in element_blob or "type password" in element_blob or "type=password" in element_blob
    auth_url = any(word in url_text for word in ("login", "signin", "sign-in", "passport", "oauth", "unified-login", "/auth"))
    login_form_text = any(word in all_text for word in ("立即登录", "密码登录", "log in", "sign in", "password login"))
    account_form_text = any(word in element_blob for word in ("phone input", "email input", "verification-code input", "submit button"))
    if has_password_field or auth_url or (login_form_text and account_form_text):
        return "login"

    if any(word in all_text for word in ("dataset card", "datasets at hugging face", "files and versions", "data studio", "resolve/main", ".csv", ".jsonl", ".parquet", ".zip")):
        return "dataset_or_resource"

    if any(word in url_text for word in ("dashboard", "console", "user-center", "account", "billing")) or any(
        word in all_text for word in ("控制台", "用户中心", "账户信息", "账号信息", "账单记录", "用量", "billing")
    ):
        return "dashboard"

    if any(word in all_text for word in ("cart", "checkout", "order", "invoice", "订单", "购物车", "账单", "支付", "发票")):
        return "cart_or_order"

    if any(word in all_text for word in ("article", "product", "details", "详情", "商品", "文章")):
        return "detail"

    if any(word in element_blob for word in ("form", "submit button", "text input", "multiline text input", "select control")):
        return "form"

    return "generic_page"


def classify_page_from_observation(observation: dict | None) -> str:
    observation = observation or {}
    return infer_page_type(
        observation.get("url", ""),
        observation.get("title", ""),
        " ".join(
            [
                observation.get("viewportText", ""),
                observation.get("pageTextPreview", ""),
                observation.get("visibleText", "")[:3000],
                observation.get("semanticTree", "")[:3000],
            ]
        ),
        observation.get("elements") or [],
        observation.get("debugSummary", ""),
    )


def semantic_tags(text: str) -> list[str]:
    haystack = (text or "").lower()
    mapping = {
        "login": ["login", "signin", "sign in", "passport", "登录", "登陆", "账号", "密码"],
        "search": ["search", "query", "搜索", "检索"],
        "form": ["form", "submit", "input", "textarea", "表单", "提交", "填写"],
        "error": ["error", "failed", "invalid", "失败", "错误", "无效", "异常"],
        "blocked": ["captcha", "verify", "blocked", "access denied", "验证码", "人机验证", "访问受限"],
        "navigation": ["navigate", "open_tab", "switch_tab", "back", "跳转", "切换", "返回", "打开"],
        "scroll": ["scroll", "滚动", "滑动"],
        "order": ["order", "billing", "invoice", "usage", "订单", "账单", "发票", "用量"],
        "product": ["product", "price", "cart", "商品", "价格", "购物车"],
    }
    tags = [tag for tag, keywords in mapping.items() if any(keyword in haystack for keyword in keywords)]
    return tags[:8]


def resource_candidate_from_element(element: dict, page_url: str) -> dict | None:
    href = str(element.get("href") or "")
    if not href.startswith(("http://", "https://")):
        return None
    parsed = urlparse(href)
    path = (parsed.path or "").lower()
    if not any(path.endswith(suffix) for suffix in RESOURCE_FILE_SUFFIXES):
        return None
    filename = parsed.path.rstrip("/").rsplit("/", 1)[-1] or "resource"
    return {
        "name": filename,
        "url": href,
        "source_page": normalize_url(page_url),
        "status": "observed",
        "evidence": compact_text(
            " ".join(
                str(element.get(key) or "")
                for key in ("text", "title", "description", "nearbyText", "containerText")
            ),
            300,
        ),
    }


def resource_reference_from_action(action: dict | None) -> dict | None:
    action = action or {}
    url = str(action.get("url") or "")
    filename = str(action.get("filename") or "")
    if action.get("action") not in {"download", "save_page"} and not filename:
        return None
    name = filename or (urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] if url else "")
    if not name:
        return None
    return {"name": name, "url": normalize_url(url)}


def error_signature(result: dict | None) -> str:
    result = result if isinstance(result, dict) else {}
    error_type = str(result.get("errorType") or "").strip().lower()
    return error_type or compact_text(result.get("message", ""), 120).lower()


def append_unique(items: list[dict], item: dict, key: str, limit: int = 20) -> None:
    value = str(item.get(key) or "").lower()
    if not value:
        return
    for index, existing in enumerate(items):
        if str(existing.get(key) or "").lower() == value:
            items[index] = {**existing, **item}
            return
    items.append(item)
    if len(items) > limit:
        del items[:-limit]


def format_action_memory_summary(fields: dict) -> str:
    return compact_text(
        " ".join(
            part
            for part in [
                fields.get("summary", ""),
                f"intent={fields.get('intent')}" if fields.get("intent") else "",
                f"outcome={fields.get('outcome')}" if fields.get("outcome") else "",
                f"progress={fields.get('progress_delta')}" if fields.get("progress_delta") else "",
                f"quality={fields.get('quality')}" if fields.get("quality") else "",
                f"lesson={fields.get('lesson')}" if fields.get("lesson") else "",
                f"retry={fields.get('retry_recommendation')}" if fields.get("retry_recommendation") else "",
                f"avoid_repeat={fields.get('avoid_repeat')}" if fields.get("avoid_repeat") not in {None, ""} else "",
                f"next={fields.get('next_hint')}" if fields.get("next_hint") else "",
            ]
            if part
        ),
        1200,
    )


def fallback_action_memory_fields(entry: dict) -> dict:
    action = entry.get("action") or {}
    result = entry.get("result") or {}
    action_name = action.get("action", "unknown")
    ok = result.get("ok") if isinstance(result, dict) else None
    outcome = "success" if ok is True else "failure" if ok is False else "neutral"
    progress_delta = "unknown"
    if action_name in {"download", "save_page", "click", "type", "press", "navigate", "open_tab", "switch_tab", "scroll"} and ok is True:
        progress_delta = "possibly_advanced"
    if ok is False:
        progress_delta = "blocked"
    quality = "poor" if ok is False else "good" if action_name in {"download", "save_page"} and ok is True else "neutral"
    retry_recommendation = "change_strategy" if ok is False else "inspect_result" if action_name == "tool" else "inspect_more" if action_name in {"download", "save_page"} else "retry"
    parts = [
        f"Action {entry.get('actionId', '')}: {action.get('action', 'unknown')}",
        f"tool={action.get('name')}" if action_name == 'tool' else '',
        f"target={action.get('target_id')}" if action.get("target_id") else "",
        f"url={action.get('url')}" if action.get("url") else "",
        f"reason={action.get('reason')}" if action.get("reason") else "",
        f"result={result.get('message')}" if result.get("message") else "",
        f"error={entry.get('error')}" if entry.get("error") else "",
        f"page={entry.get('snippet')}" if entry.get("snippet") else "",
    ]
    return {
        "summary": compact_text("; ".join(part for part in parts if part), 700),
        "intent": compact_text(action.get("page_summary") or action.get("reason") or f"execute {action_name}", 260),
        "outcome": outcome,
        "progress_delta": progress_delta,
        "quality": quality,
        "evidence": compact_text(result.get("message") if isinstance(result, dict) else result, 260),
        "quality_reason": compact_text(
            "Action failed and did not produce a usable result." if ok is False
            else "A local artifact was acquired." if quality == "good"
            else "Execution result alone does not prove meaningful task progress.",
            260,
        ),
        "retry_recommendation": retry_recommendation,
        "lesson": compact_text(result.get("message") if isinstance(result, dict) else result, 260),
        "avoid_repeat": bool(ok is False),
        "next_hint": "",
    }


def normalize_action_memory_fields(parsed: dict, fallback: dict) -> dict:
    fields = {
        "summary": compact_text(parsed.get("summary") or fallback.get("summary"), 700),
        "intent": compact_text(parsed.get("intent") or fallback.get("intent"), 320),
        "outcome": compact_text(parsed.get("outcome") or fallback.get("outcome") or "neutral", 80),
        "progress_delta": compact_text(parsed.get("progress_delta") or parsed.get("progress") or fallback.get("progress_delta"), 160),
        "quality": compact_text(parsed.get("quality") or fallback.get("quality") or "neutral", 80),
        "evidence": compact_text(parsed.get("evidence") or fallback.get("evidence"), 360),
        "quality_reason": compact_text(parsed.get("reason") or parsed.get("quality_reason") or fallback.get("quality_reason"), 360),
        "retry_recommendation": compact_text(
            parsed.get("retry_recommendation") or fallback.get("retry_recommendation") or "inspect_more",
            100,
        ),
        "lesson": compact_text(parsed.get("lesson") or fallback.get("lesson"), 360),
        "avoid_repeat": parsed.get("avoid_repeat", fallback.get("avoid_repeat", False)),
        "next_hint": compact_text(parsed.get("next_hint") or parsed.get("next_relevance") or fallback.get("next_hint"), 360),
    }
    if isinstance(fields["avoid_repeat"], str):
        fields["avoid_repeat"] = fields["avoid_repeat"].strip().lower() in {"true", "yes", "1", "avoid", "避免"}
    fields["summary_text"] = format_action_memory_summary(fields)
    return fields


async def summarize_action_for_memory(task: str, entry: dict, model_settings: dict) -> dict:
    fallback = fallback_action_memory_fields(entry)
    if model_settings.get("memoryUseModelSummaries") is False:
        return normalize_action_memory_fields({}, fallback)

    safe_action = redact_action_payload(entry.get("action") or {})
    payload = {
        "task": task,
        "action_id": entry.get("actionId"),
        "step": entry.get("step"),
        "phase": entry.get("phase"),
        "page": {
            "url": entry.get("url"),
            "title": entry.get("title"),
            "snippet": entry.get("snippet"),
        },
        "action": json_preview(safe_action, 1800),
        "result": json_preview(entry.get("result"), 1000),
        "error": json_preview(entry.get("error"), 900),
        "strategy_reviews": entry.get("strategyReviews") or [],
        "sourceCount": entry.get("sourceCount"),
        "usableSourceCount": entry.get("usableSourceCount"),
    }
    options = {**model_settings, "maxTokens": 384, "temperature": 0.1, "enableThinking": False}
    raw = await chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "Summarize one browser-agent action for future planning. Output JSON only: "
                    "{\"summary\":\"...\",\"intent\":\"...\",\"outcome\":\"success|failure|neutral\","
                    "\"progress_delta\":\"advanced|blocked|regressed|no_change|unknown\","
                    "\"quality\":\"good|neutral|poor\",\"evidence\":\"...\",\"reason\":\"...\","
                    "\"retry_recommendation\":\"retry|avoid|inspect_more|change_strategy\","
                    "\"lesson\":\"...\",\"avoid_repeat\":true|false,\"next_hint\":\"...\"}. "
                    "Keep fields factual and concise. Preserve useful URLs, element IDs/selectors when visible. "
                    "Explicitly state whether the action advanced the task, failed, repeated a bad path, or produced a lesson for the next step."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        options,
    )
    parsed = parse_json_object(raw)
    return normalize_action_memory_fields(parsed, fallback)


async def summarize_memory_chunk(task: str, items: list[dict], model_settings: dict) -> str:
    if model_settings.get("memoryUseModelSummaries") is False:
        return fallback_chunk_summary(items)

    payload = {
        "task": task,
        "action_ids": [item.get("actionId") for item in items if item.get("actionId")],
        "steps": [item.get("step") for item in items if item.get("step")],
        "summaries": [
            {
                "actionId": item.get("actionId"),
                "step": item.get("step"),
                "url": item.get("url"),
                "title": item.get("title"),
                "action": item.get("action"),
                "result": item.get("result"),
                "success": item.get("success"),
                "summary": item.get("summary"),
                "intent": item.get("intent"),
                "outcome": item.get("outcome"),
                "progressDelta": item.get("progressDelta"),
                "quality": item.get("quality"),
                "retryRecommendation": item.get("retryRecommendation"),
                "lesson": item.get("lesson"),
                "avoidRepeat": item.get("avoidRepeat"),
                "nextHint": item.get("nextHint"),
            }
            for item in items
        ],
    }
    options = {**model_settings, "maxTokens": 512, "temperature": 0.1, "enableThinking": False}
    raw = await chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "Compress several browser-agent action memories into one durable summary index. "
                    "Output JSON only: {\"summary\":\"...\",\"outcome\":\"success|failure|mixed|neutral\",\"next_relevance\":\"...\"}. "
                    "Do not delete details conceptually: keep references to important action IDs, useful URLs, failures, "
                    "selectors/element IDs, repeated loops, blocked/login/network pages, and what should or should not be retried."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        options,
    )
    parsed = parse_json_object(raw)
    return compact_text(
        " ".join(
            part
            for part in [
                parsed.get("summary", ""),
                f"outcome={parsed.get('outcome')}" if parsed.get("outcome") else "",
                f"next={parsed.get('next_relevance')}" if parsed.get("next_relevance") else "",
            ]
            if part
        ),
        1200,
    )


def fallback_chunk_summary(items: list[dict]) -> str:
    ids = ", ".join(item.get("actionId", "") for item in items if item.get("actionId"))
    text = " | ".join(
        f"{item.get('actionId', '')} step={item.get('step', '')} {item.get('action', '')} {item.get('url', '')}: {item.get('summary') or item.get('result') or ''}"
        for item in items
    )
    return compact_text(f"Compressed action summary index for {ids}: {text}", 1200)


@dataclass
class BrowserAgentMemory:
    agent_settings: dict = field(default_factory=dict)
    model_settings: dict = field(default_factory=dict)
    raw_archive: list[dict] = field(default_factory=list)
    action_summaries: list[dict] = field(default_factory=list)
    summary_chunks: list[dict] = field(default_factory=list)
    task_state: dict = field(default_factory=dict)
    _next_chunk_start: int = 0

    def snapshot(self):
        return deepcopy({key: getattr(self, key) for key in
            ("raw_archive", "action_summaries", "summary_chunks", "task_state", "_next_chunk_start")})

    def restore(self, snapshot, *, history=None):
        for key in ("raw_archive", "action_summaries", "summary_chunks", "task_state", "_next_chunk_start"):
            if key in snapshot:
                setattr(self, key, deepcopy(snapshot[key]))
        self._trim_archive()
        # Upgrade older checkpoints from their original evidence, never by guessing
        # content that was absent from the old text-only projection.
        originals = {entry.get('actionId'): entry for entry in history or []}
        for record in self.raw_archive:
            original = originals.get(record.get('actionId'))
            if original and record.get('action') == 'tool' and not record.get('resultObservation'):
                record['resultObservation'] = bounded_result_observation(original.get('result'), record['actionId'])
                record['result'] = compact_text(record['resultObservation']['view'], 700)

    def __post_init__(self) -> None:
        self.refresh_budget()
        self.archive_max_items = int(self.agent_settings.get("memory_archive_max_items", 1000))
        self.recent_min = int(self.agent_settings.get("memory_recent_min", 6))
        self.recent_max = int(self.agent_settings.get("memory_recent_max", 40))
        self.recall_max_items = int(self.agent_settings.get("memory_recall_max_items", 8))
        self.summary_max_items = int(self.agent_settings.get("memory_summary_max_items", 12))
        self.summary_chunk_size = int(self.agent_settings.get("memory_summary_chunk_size", 8))
        self.summary_max_chunks = int(self.agent_settings.get("memory_summary_max_chunks", 80))
        self.task_state_enabled = bool(self.agent_settings.get("memory_task_state_enabled", True))
        self.action_quality_enabled = bool(self.agent_settings.get("memory_action_quality_enabled", True))
        self.run_brief_enabled = bool(self.agent_settings.get("memory_run_brief_enabled", True))
        self.structured_recall_enabled = bool(self.agent_settings.get("memory_structured_recall_enabled", True))

    def refresh_budget(self):
        self.budget = ContextBudget.from_settings(self.model_settings, self.agent_settings, RUN_METRICS.get())
        self.context_window_tokens = self.budget.context_window_tokens
        self.reserved_output_tokens = self.budget.reserved_output_tokens
        self.prompt_budget_ratio = self.budget.prompt_budget_ratio
        self.chars_per_token = self.budget.chars_per_token

    async def add_entry(self, entry: dict, task: str, model_settings: dict | None = None) -> dict | None:
        if not entry:
            return None

        settings = {**self.model_settings, **(model_settings or {})}
        if "memoryUseModelSummaries" not in settings:
            settings["memoryUseModelSummaries"] = bool(self.agent_settings.get("memory_use_model_summaries", True))

        try:
            memory_fields = await summarize_action_for_memory(task, entry, settings)
        except Exception:
            memory_fields = normalize_action_memory_fields({}, fallback_action_memory_fields(entry))

        record = self._make_record(entry, memory_fields)
        self.raw_archive.append(record)
        self.action_summaries.append(self._compact_summary_record(record))
        if self.task_state_enabled:
            self._update_task_state_from_record(task, record, entry)
        self._trim_archive()
        await self._build_summary_chunks_if_ready(task, settings)
        return record

    def _make_record(self, entry: dict, memory_fields: dict) -> dict:
        action = redact_action_payload(entry.get("action") or {})
        result = entry.get("result") or {}
        url = normalize_url(entry.get("url", ""))
        title = entry.get("title", "")
        snippet = compact_text(entry.get("snippet", ""), 1600)
        action_text = json_preview(action, 1400)
        result_observation = bounded_result_observation(result, entry.get('actionId', '')) if action.get('action') == 'tool' else None
        result_text = compact_text(result_observation['view'] if result_observation else result.get("message") or result, 700)
        page_type = entry.get("pageType") or infer_page_type(url, title, snippet)
        success = result.get("ok") if isinstance(result, dict) else None
        summary = memory_fields.get("summary_text") or format_action_memory_summary(memory_fields)
        searchable_text = " ".join(
            [
                url,
                title,
                snippet,
                action_text,
                result_text,
                summary,
                page_type,
                str(entry.get("phase") or ""),
            ]
        )
        tags = list(dict.fromkeys([page_type, action.get("action", ""), *semantic_tags(searchable_text), *tokenize(host(url))[:4]]))
        target = action.get("target_id") or action.get("url") or action.get("key") or action.get("message") or action.get('name') or ""
        resource_reference = resource_reference_from_action(action)
        return {
            "actionId": entry.get("actionId", ""),
            "step": entry.get("step", ""),
            "phase": entry.get("phase", ""),
            "url": url,
            "host": host(url),
            "pathPrefix": path_prefix(url),
            "title": title,
            "pageType": page_type,
            "taskPhase": self.task_state.get("current_phase", "") if self.task_state_enabled else "",
            "action": action.get("action", ""),
            "target": target,
            "reason": action.get("reason", ""),
            "result": result_text,
            "resultObservation": result_observation,
            "artifact": result.get("artifact") if isinstance(result, dict) else None,
            "success": success,
            "errorType": result.get("errorType", "") if isinstance(result, dict) else "",
            "errorSignature": error_signature(result),
            "summary": summary,
            "intent": memory_fields.get("intent", ""),
            "outcome": memory_fields.get("outcome", ""),
            "progressDelta": memory_fields.get("progress_delta", ""),
            "quality": memory_fields.get("quality", ""),
            "qualityEvidence": memory_fields.get("evidence", ""),
            "qualityReason": memory_fields.get("quality_reason", ""),
            "retryRecommendation": memory_fields.get("retry_recommendation", ""),
            "lesson": memory_fields.get("lesson", ""),
            "avoidRepeat": bool(memory_fields.get("avoid_repeat", False)),
            "nextHint": memory_fields.get("next_hint", ""),
            "memoryFields": memory_fields,
            "snippet": snippet,
            "actionPayload": action,
            "resourceReferences": [resource_reference] if resource_reference else [],
            "sourceCount": entry.get("sourceCount", ""),
            "usableSourceCount": entry.get("usableSourceCount", ""),
            "timestamp": entry.get("timestamp", ""),
            "tags": tags[:12],
            "terms": dict(Counter(tokenize(searchable_text)).most_common(80)),
            "importance": self._importance_for_entry(entry, summary, page_type),
        }

    def _importance_for_entry(self, entry: dict, summary: str, page_type: str) -> float:
        result = entry.get("result") or {}
        action = entry.get("action") or {}
        score = 1.0
        if result.get("ok") is False:
            score += 0.6
        if page_type in {"login", "dashboard", "error_or_blocked", "cart_or_order"}:
            score += 0.5
        if action.get("action") in {"navigate", "open_tab", "switch_tab", "back"}:
            score += 0.2
        if any(tag in (summary or "").lower() for tag in ["success", "failure", "blocked", "登录", "失败", "成功", "验证码"]):
            score += 0.3
        return min(score, 3.0)

    def _compact_summary_record(self, record: dict) -> dict:
        return {
            "actionId": record.get("actionId", ""),
            "step": record.get("step", ""),
            "phase": record.get("phase", ""),
            "url": record.get("url", ""),
            "title": record.get("title", ""),
            "pageType": record.get("pageType", ""),
            "taskPhase": record.get("taskPhase", ""),
            "action": record.get("action", ""),
            "target": record.get("target", ""),
            "result": record.get("result", ""),
            "artifact": record.get("artifact"),
            "success": record.get("success"),
            "errorType": record.get("errorType", ""),
            "errorSignature": record.get("errorSignature", ""),
            "summary": compact_text(record.get("summary"), 900),
            "intent": compact_text(record.get("intent"), 320),
            "outcome": record.get("outcome", ""),
            "progressDelta": record.get("progressDelta", ""),
            "quality": record.get("quality", ""),
            "qualityEvidence": compact_text(record.get("qualityEvidence"), 360),
            "qualityReason": compact_text(record.get("qualityReason"), 360),
            "retryRecommendation": record.get("retryRecommendation", ""),
            "lesson": compact_text(record.get("lesson"), 360),
            "avoidRepeat": bool(record.get("avoidRepeat", False)),
            "nextHint": compact_text(record.get("nextHint"), 360),
            "resourceReferences": record.get("resourceReferences", [])[:6],
            "tags": record.get("tags", [])[:10],
            "importance": record.get("importance", 1.0),
            "sourceCount": record.get("sourceCount", ""),
            "usableSourceCount": record.get("usableSourceCount", ""),
            "timestamp": record.get("timestamp", ""),
        }

    def _trim_archive(self) -> None:
        if self.archive_max_items <= 0:
            return
        if len(self.raw_archive) <= self.archive_max_items:
            return
        overflow = len(self.raw_archive) - self.archive_max_items
        self.raw_archive = self.raw_archive[overflow:]
        self.action_summaries = self.action_summaries[overflow:]
        self._next_chunk_start = max(0, self._next_chunk_start - overflow)
        # Summary chunks remain valid indexes for older steps even if in-memory raw archive is capped.

    async def _build_summary_chunks_if_ready(self, task: str, model_settings: dict) -> None:
        while len(self.action_summaries) - self._next_chunk_start >= max(self.summary_chunk_size, 2):
            end = self._next_chunk_start + self.summary_chunk_size
            items = self.action_summaries[self._next_chunk_start : end]
            try:
                summary = await summarize_memory_chunk(task, items, model_settings)
            except Exception:
                summary = fallback_chunk_summary(items)

            action_ids = [item.get("actionId") for item in items if item.get("actionId")]
            step_ids = [item.get("step") for item in items if item.get("step") != ""]
            urls = list(dict.fromkeys(item.get("url", "") for item in items if item.get("url")))[:8]
            page_types = list(dict.fromkeys(item.get("pageType", "") for item in items if item.get("pageType")))[:8]
            tags = list(dict.fromkeys(tag for item in items for tag in (item.get("tags") or [])))[:16]
            chunk_text = " ".join([summary, " ".join(urls), " ".join(page_types), " ".join(tags)])
            self.summary_chunks.append(
                {
                    "chunkId": f"{action_ids[0] if action_ids else self._next_chunk_start}-{action_ids[-1] if action_ids else end}",
                    "actionIds": action_ids,
                    "steps": step_ids,
                    "summary": summary,
                    "urls": urls,
                    "pageTypes": page_types,
                    "tags": tags,
                    "importance": max((float(item.get("importance") or 1.0) for item in items), default=1.0),
                    "terms": dict(Counter(tokenize(chunk_text)).most_common(100)),
                }
            )
            self._next_chunk_start = end
            if len(self.summary_chunks) > self.summary_max_chunks:
                self.summary_chunks = self.summary_chunks[-self.summary_max_chunks :]

    def to_legacy_memory(self) -> list[dict]:
        # Keeps old policy/planner compatibility without exposing the entire raw archive.
        return self.action_summaries[-200:]

    def stats(self) -> dict:
        return {
            "raw_archive": len(self.raw_archive),
            "action_summaries": len(self.action_summaries),
            "summary_chunks": len(self.summary_chunks),
            "context_window_tokens": self.context_window_tokens,
            "recent_min": self.recent_min,
            "recent_max": self.recent_max,
            "task_state_enabled": self.task_state_enabled,
            "task_phase": self.task_state.get("current_phase", "") if self.task_state_enabled else "",
            "action_quality_enabled": self.action_quality_enabled,
            "run_brief_enabled": self.run_brief_enabled,
            "structured_recall_enabled": self.structured_recall_enabled,
        }

    def build_context(
        self,
        task: str,
        observation: dict | None = None,
        last_result: dict | None = None,
        sources: list[dict] | None = None,
        base_payload: dict | None = None,
    ) -> dict:
        observation = observation or {}
        if self.task_state_enabled:
            self._update_task_state_from_observation(task, observation, sources or [])
        query_profile = self._query_profile(task, observation, last_result)
        budget = self._budget_for(base_payload or {}, observation)

        recent_records = self._select_recent_records(budget["recent_exact_budget_tokens"])
        recent_ids = {record.get("actionId") for record in recent_records}

        recalled_records = self._select_recalled_records(
            query_profile,
            exclude_action_ids=recent_ids,
            token_budget=budget["recall_budget_tokens"],
        )
        recalled_ids = {record.get("actionId") for record in recalled_records}

        summary_chunks = self._select_summary_chunks(
            query_profile,
            exclude_action_ids=recent_ids | recalled_ids,
            token_budget=budget["summary_budget_tokens"],
        )

        lessons = self._derive_lessons(query_profile)
        task_memory = self._derive_task_memory(task, observation, last_result, recent_records, recalled_records, lessons)
        run_memory_brief = self._derive_run_memory_brief(task) if self.run_brief_enabled else {}

        memory_context = {
            "memory_policy": {
                "strategy": "adaptive_budgeted_archive_recall",
                "description": (
                    "Raw action history is archived. The prompt receives dynamic recent exact memory, "
                    "recalled relevant exact history, and compact summary indexes within the estimated context budget."
                ),
            },
            "task_memory": task_memory,
            "run_memory_brief": run_memory_brief,
            "recent_exact_history": [self._format_exact_record(record) for record in recent_records],
            "recalled_relevant_history": [self._format_recalled_record(record, query_profile) for record in recalled_records],
            "compressed_action_memory": [self._format_summary_chunk(chunk) for chunk in summary_chunks],
            "context_budget": budget,
            "archive_stats": self.stats(),
            "estimated_memory_tokens": int(budget.get("memory_budget_tokens") or 128),
        }
        self._fit_context_to_budget(memory_context, budget)
        for _ in range(3):
            estimated_tokens = estimate_tokens(memory_context, self.chars_per_token)
            if memory_context["estimated_memory_tokens"] == estimated_tokens:
                break
            memory_context["estimated_memory_tokens"] = estimated_tokens
        return memory_context

    def _budget_for(self, base_payload: dict, observation: dict) -> dict:
        self.refresh_budget()
        available = self.budget.available_prompt_tokens
        base_tokens = estimate_tokens(base_payload, self.chars_per_token)
        observation_tokens = estimate_tokens(
            {
                "url": observation.get("url", ""),
                "title": observation.get("title", ""),
                "viewportText": (observation.get("viewportText") or "")[:6000],
                "semanticTree": (observation.get("semanticTree") or observation.get("cleanedHtml") or "")[:9000],
                "visibleText": (observation.get("visibleText") or "")[:10000],
                "elements": (observation.get("elements") or [])[:120],
            },
            self.chars_per_token,
        )
        # The base payload already includes compact observation in planner, but this extra estimate
        # prevents memory from expanding aggressively on very large pages.
        base_estimate = max(base_tokens, observation_tokens)
        remaining = available - base_estimate
        memory_budget = max(128, remaining) if remaining > 0 else 128
        recent_budget = max(256, int(memory_budget * 0.48))
        recall_budget = max(192, int(memory_budget * 0.34))
        summary_budget = max(128, memory_budget - recent_budget - recall_budget)
        pressure = "high" if memory_budget < 1200 else "medium" if memory_budget < 3000 else "low"
        return {
            **self.budget.as_dict(),
            "reserved_output_tokens": self.reserved_output_tokens,
            "prompt_budget_ratio": self.prompt_budget_ratio,
            "available_prompt_tokens": available,
            "estimated_base_tokens": base_estimate,
            "memory_budget_tokens": memory_budget,
            "recent_exact_budget_tokens": recent_budget,
            "recall_budget_tokens": recall_budget,
            "summary_budget_tokens": summary_budget,
            "pressure": pressure,
            "chars_per_token": self.chars_per_token,
        }

    def _fit_context_to_budget(self, memory_context: dict, budget: dict) -> None:
        target_tokens = max(128, int(budget.get("memory_budget_tokens") or 128))

        def current_tokens() -> int:
            return estimate_tokens(memory_context, self.chars_per_token)

        while current_tokens() > target_tokens:
            compressed = memory_context.get("compressed_action_memory") or []
            recalled = memory_context.get("recalled_relevant_history") or []
            recent = memory_context.get("recent_exact_history") or []
            facts = (memory_context.get("task_memory") or {}).get("important_facts") or []
            completed = (memory_context.get("task_memory") or {}).get("completed_recent") or []
            task_memory = memory_context.get("task_memory") or {}
            brief = memory_context.get("run_memory_brief") or {}

            if compressed:
                compressed.pop(0)
            elif recalled:
                recalled.pop(0)
            elif len(recent) > max(1, min(self.recent_min, 2)):
                recent.pop(0)
            elif facts:
                facts.pop(0)
            elif completed:
                completed.pop(0)
            elif brief.get("known_candidates"):
                brief["known_candidates"].pop(0)
            elif brief.get("decision_points"):
                brief["decision_points"].pop(0)
            elif brief.get("recent_mistakes"):
                brief["recent_mistakes"].pop(0)
            elif brief.get("successful_path"):
                brief["successful_path"].pop(0)
            elif task_memory.get("strategy_feedback"):
                task_memory["strategy_feedback"].pop(0)
            elif task_memory.get("action_quality"):
                task_memory["action_quality"].pop(0)
            elif task_memory.get("decision_points"):
                task_memory["decision_points"].pop(0)
            elif task_memory.get("recent_mistakes"):
                task_memory["recent_mistakes"].pop(0)
            elif task_memory.get("effective_actions"):
                task_memory["effective_actions"].pop(0)
            elif task_memory.get("candidate_resources"):
                task_memory["candidate_resources"].pop(0)
            elif task_memory.get("candidate_sources"):
                task_memory["candidate_sources"].pop(0)
            elif task_memory.get("verified_facts"):
                task_memory["verified_facts"].pop(0)
            elif task_memory.get("failed_paths"):
                task_memory["failed_paths"].pop(0)
            elif len(recent) > 1:
                recent.pop(0)
            else:
                break

    def _query_profile(self, task: str, observation: dict, last_result: dict | None) -> dict:
        url = normalize_url(observation.get("url", ""))
        title = observation.get("title", "")
        visible = " ".join(
            [
                observation.get("viewportText", ""),
                observation.get("pageTextPreview", ""),
                observation.get("visibleText", "")[:3000],
                observation.get("semanticTree", "")[:3000],
                json_preview(last_result, 500),
            ]
        )
        text = " ".join([task or "", url, title, visible])
        page_type = (observation.get("pageType") if isinstance(observation, dict) else "") or infer_page_type(
            url,
            title,
            visible,
            observation.get("elements") if isinstance(observation, dict) else [],
            observation.get("debugSummary", "") if isinstance(observation, dict) else "",
        )
        terms = Counter(tokenize(text))
        tags = list(dict.fromkeys([page_type, *semantic_tags(text), *tokenize(host(url))[:4]]))
        candidate_resources: list[dict] = []
        for element in observation.get("elements") or []:
            candidate = resource_candidate_from_element(element, url)
            if candidate:
                append_unique(candidate_resources, candidate, "url", limit=30)
        return {
            "task": task,
            "url": url,
            "host": host(url),
            "pathPrefix": path_prefix(url),
            "title": title,
            "pageType": page_type,
            "taskPhase": self.task_state.get("current_phase", "") if self.task_state_enabled else "",
            "visibleTargets": {
                str(element.get("id") or "")
                for element in (observation.get("elements") or [])
                if element.get("id")
            },
            "candidateResources": candidate_resources,
            "lastFailureSignature": error_signature(last_result) if last_result and last_result.get("ok") is False else "",
            "terms": terms,
            "tags": tags,
            "text": text,
        }

    def _record_score(self, record: dict, query: dict) -> float:
        score = 0.0
        if record.get("url") and record.get("url") == query.get("url"):
            score += 5.0
        if record.get("host") and record.get("host") == query.get("host"):
            score += 2.5
        if record.get("pathPrefix") and record.get("pathPrefix") == query.get("pathPrefix"):
            score += 1.0
        if record.get("pageType") and record.get("pageType") == query.get("pageType"):
            score += 2.0
        if self.structured_recall_enabled:
            if record.get("taskPhase") and record.get("taskPhase") == query.get("taskPhase"):
                score += 2.2
            if record.get("target") and record.get("target") in query.get("visibleTargets", set()):
                score += 1.3
            if (
                record.get("errorSignature")
                and query.get("lastFailureSignature")
                and record.get("errorSignature") == query.get("lastFailureSignature")
            ):
                score += 4.0
            record_resources = {
                str(item.get("name") or "").lower()
                for item in (record.get("resourceReferences") or [])
                if item.get("name")
            }
            query_resources = {
                str(item.get("name") or "").lower()
                for item in (query.get("candidateResources") or [])
                if item.get("name")
            }
            if record_resources.intersection(query_resources):
                score += 3.5

        record_terms = record.get("terms") or {}
        query_terms = query.get("terms") or {}
        overlap = set(record_terms).intersection(query_terms)
        if overlap:
            weighted = sum(min(float(record_terms.get(term, 0)), float(query_terms.get(term, 0))) for term in overlap)
            score += min(weighted * 0.35, 5.0)

        tag_overlap = set(record.get("tags") or []).intersection(query.get("tags") or [])
        score += min(len(tag_overlap) * 0.7, 3.0)

        if record.get("success") is False:
            score += 0.4
        if record.get("importance"):
            score += min(float(record.get("importance") or 0) * 0.4, 1.2)

        try:
            age = max(len(self.raw_archive) - self.raw_archive.index(record), 0)
            score -= min(age * 0.01, 1.0)
        except ValueError:
            pass
        return score

    def _chunk_score(self, chunk: dict, query: dict) -> float:
        score = 0.0
        if set(chunk.get("pageTypes") or []).intersection({query.get("pageType")}):
            score += 1.5
        if set(chunk.get("tags") or []).intersection(query.get("tags") or []):
            score += len(set(chunk.get("tags") or []).intersection(query.get("tags") or [])) * 0.5
        chunk_terms = chunk.get("terms") or {}
        query_terms = query.get("terms") or {}
        overlap = set(chunk_terms).intersection(query_terms)
        if overlap:
            weighted = sum(min(float(chunk_terms.get(term, 0)), float(query_terms.get(term, 0))) for term in overlap)
            score += min(weighted * 0.25, 4.0)
        score += min(float(chunk.get("importance") or 1.0) * 0.25, 0.8)
        return score

    def _select_recent_records(self, token_budget: int) -> list[dict]:
        if not self.raw_archive:
            return []
        max_count = min(max(self.recent_max, self.recent_min), len(self.raw_archive))
        min_count = min(self.recent_min, len(self.raw_archive))
        for count in range(max_count, min_count - 1, -1):
            records = self.raw_archive[-count:]
            if estimate_tokens([self._format_exact_record(record) for record in records], self.chars_per_token) <= token_budget:
                return records
        return self.raw_archive[-min_count:]

    def _select_recalled_records(self, query: dict, exclude_action_ids: set[str], token_budget: int) -> list[dict]:
        candidates = []
        recent_exclusion = set(exclude_action_ids or set())
        for record in self.raw_archive:
            action_id = record.get("actionId")
            if action_id in recent_exclusion:
                continue
            score = self._record_score(record, query)
            if score >= 1.2:
                candidates.append((score, record))
        candidates.sort(key=lambda pair: (pair[0], pair[1].get("step") or 0), reverse=True)

        selected: list[dict] = []
        for score, record in candidates:
            candidate = [*selected, record]
            if len(candidate) > self.recall_max_items:
                break
            formatted = [self._format_recalled_record(item, query) for item in candidate]
            if estimate_tokens(formatted, self.chars_per_token) > token_budget and selected:
                continue
            selected.append(record)
        selected.sort(key=lambda item: item.get("step") or 0)
        return selected

    def _select_summary_chunks(self, query: dict, exclude_action_ids: set[str], token_budget: int) -> list[dict]:
        candidates = []
        for chunk in self.summary_chunks:
            action_ids = set(chunk.get("actionIds") or [])
            if action_ids and action_ids.intersection(exclude_action_ids):
                continue
            score = self._chunk_score(chunk, query)
            if score >= 0.8:
                candidates.append((score, chunk))
        if not candidates:
            candidates = [(0.1, chunk) for chunk in self.summary_chunks[-self.summary_max_items :]]
        candidates.sort(key=lambda pair: pair[0], reverse=True)

        selected: list[dict] = []
        for _, chunk in candidates:
            candidate = [*selected, chunk]
            if len(candidate) > self.summary_max_items:
                break
            formatted = [self._format_summary_chunk(item) for item in candidate]
            if estimate_tokens(formatted, self.chars_per_token) > token_budget and selected:
                continue
            selected.append(chunk)
        selected.sort(key=lambda item: (item.get("steps") or [0])[0] if item.get("steps") else 0)
        return selected

    def _format_exact_record(self, record: dict) -> dict:
        action = redact_action_payload(record.get('actionPayload') or {})
        encoded = json.dumps(action, ensure_ascii=False, default=str)
        action_view = action if len(encoded) <= 2400 else {
            'action': action.get('action'), 'name': action.get('name'),
            'arguments_preview': encoded[:1600], 'truncated': True,
            'message': 'Use history_read with action_id for the full original call.'}
        return {
            "artifact": record.get("artifact"),
            "action_id": record.get("actionId", ""),
            "step": record.get("step", ""),
            "url": record.get("url", ""),
            "title": record.get("title", ""),
            "page_type": record.get("pageType", ""),
            "task_phase": record.get("taskPhase", ""),
            "action": record.get("action", ""),
            "action_payload": action_view,
            "target": record.get("target", ""),
            "success": record.get("success"),
            "error_type": record.get("errorType", ""),
            "error_signature": record.get("errorSignature", ""),
            "result": compact_text(record.get("result", ""), 500),
            **({'result_observation': deepcopy(record['resultObservation'])} if record.get('resultObservation') else {}),
            "summary": compact_text(record.get("summary", ""), 900),
            "intent": compact_text(record.get("intent", ""), 320),
            "outcome": record.get("outcome", ""),
            "progress_delta": record.get("progressDelta", ""),
            "quality": record.get("quality", ""),
            "quality_evidence": compact_text(record.get("qualityEvidence", ""), 360),
            "quality_reason": compact_text(record.get("qualityReason", ""), 360),
            "retry_recommendation": record.get("retryRecommendation", ""),
            "lesson": compact_text(record.get("lesson", ""), 360),
            "avoid_repeat": bool(record.get("avoidRepeat", False)),
            "next_hint": compact_text(record.get("nextHint", ""), 360),
            "page_snippet": compact_text(record.get("snippet", ""), 700),
            "tags": record.get("tags", [])[:8],
            "resource_references": record.get("resourceReferences", [])[:6],
        }

    def _format_recalled_record(self, record: dict, query: dict) -> dict:
        reasons = self._recall_reasons(record, query)
        return {
            **self._format_exact_record(record),
            "reason_for_recall": reasons,
            "why_recalled": "; ".join(reasons) or "relevant_by_text_similarity",
            "recommended_use": self._recommended_recall_use(record, reasons),
        }

    def _format_summary_chunk(self, chunk: dict) -> dict:
        return {
            "chunk_id": chunk.get("chunkId", ""),
            "action_ids": chunk.get("actionIds", []),
            "steps": chunk.get("steps", []),
            "summary": compact_text(chunk.get("summary", ""), 1200),
            "urls": chunk.get("urls", [])[:6],
            "page_types": chunk.get("pageTypes", [])[:6],
            "tags": chunk.get("tags", [])[:12],
        }

    def _recall_reasons(self, record: dict, query: dict) -> list[str]:
        reasons = []
        if record.get("url") == query.get("url") and record.get("url"):
            reasons.append("same_url")
        elif record.get("host") == query.get("host") and record.get("host"):
            reasons.append("same_host")
        if record.get("pageType") == query.get("pageType"):
            reasons.append(f"same_page_type:{record.get('pageType')}")
        if self.structured_recall_enabled:
            if record.get("taskPhase") and record.get("taskPhase") == query.get("taskPhase"):
                reasons.append(f"same_phase:{record.get('taskPhase')}")
            if record.get("target") and record.get("target") in query.get("visibleTargets", set()):
                reasons.append(f"same_visible_target:{record.get('target')}")
            if (
                record.get("errorSignature")
                and query.get("lastFailureSignature")
                and record.get("errorSignature") == query.get("lastFailureSignature")
            ):
                reasons.append(f"same_failure_type:{record.get('errorSignature')}")
            record_names = {
                str(item.get("name") or "").lower()
                for item in (record.get("resourceReferences") or [])
                if item.get("name")
            }
            query_names = {
                str(item.get("name") or "").lower()
                for item in (query.get("candidateResources") or [])
                if item.get("name")
            }
            for name in sorted(record_names.intersection(query_names))[:3]:
                reasons.append(f"same_candidate_resource:{name}")
        tag_overlap = set(record.get("tags") or []).intersection(query.get("tags") or [])
        if tag_overlap:
            reasons.append("tags:" + ",".join(sorted(tag_overlap)[:4]))
        term_overlap = set(record.get("terms") or {}).intersection(query.get("terms") or {})
        if term_overlap:
            reasons.append("terms:" + ",".join(sorted(term_overlap)[:5]))
        if record.get("success") is False:
            reasons.append("previous_failure")
        return reasons[:8]

    def _explain_recall(self, record: dict, query: dict) -> str:
        return "; ".join(self._recall_reasons(record, query)) or "relevant_by_text_similarity"

    def _recommended_recall_use(self, record: dict, reasons: list[str]) -> str:
        if record.get("success") is False or record.get("avoidRepeat"):
            return "avoid"
        if any(reason.startswith("same_candidate_resource:") for reason in reasons):
            return "verify" if record.get("action") in {"download", "save_page"} else "compare"
        if record.get("success") is True:
            return "reuse"
        return "compare"

    def _derive_lessons(self, query: dict) -> list[dict]:
        failures = [record for record in self.raw_archive if record.get("success") is False or record.get("errorType")]
        scored = [(self._record_score(record, query), record) for record in failures]
        scored = [(score, record) for score, record in scored if score >= 0.8]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        lessons = []
        for _, record in scored[:6]:
            lessons.append(
                {
                    "from_action_id": record.get("actionId"),
                    "context": compact_text(f"{record.get('pageType')} {record.get('url')} {record.get('title')}", 240),
                    "avoid_or_note": compact_text(record.get("summary") or record.get("result"), 500),
                }
            )
        return lessons

    def _ensure_task_state(self, task: str) -> dict:
        if self.task_state.get("goal") == task:
            return self.task_state
        self.task_state = {
            "goal": task,
            "current_phase": "locating_source",
            "completed": [],
            "active_path": "",
            "failed_paths": [],
            "candidate_sources": [],
            "candidate_resources": [],
            "verified_facts": [],
            "local_artifacts": [],
            "open_questions": [],
            "blockers": [],
            "strategy_feedback": [],
            "decision_points": [],
            "action_quality": [],
            "recent_mistakes": [],
            "effective_actions": [],
            "next_best_action_hint": "Locate a relevant public source and inspect page evidence before acquiring content.",
        }
        return self.task_state

    def record_strategy_evaluation(self, task: str, step: int, action: dict, evaluation: dict) -> dict:
        state = self._ensure_task_state(task)
        level = str(evaluation.get("level") or "suggest")
        review = {
            "review_id": f"E{step:04d}-{len(state['strategy_feedback']) + 1:02d}",
            "step": step,
            "action": action.get("action", ""),
            "target": action.get("target_id") or action.get("url") or "",
            "level": level,
            "approved": bool(evaluation.get("approved", level == "suggest")),
            "feedback": compact_text(evaluation.get("feedback", ""), 420),
            "suggested_action": compact_text(evaluation.get("suggested_action", ""), 320),
        }
        if not self.task_state_enabled or not self.action_quality_enabled:
            return review
        state["strategy_feedback"].append(review)
        state["strategy_feedback"] = state["strategy_feedback"][-30:]
        if level in {"should", "forbid"}:
            decision = {
                "review_id": review["review_id"],
                "step": step,
                "proposed_action": review["action"],
                "target": review["target"],
                "level": level,
                "reason": review["feedback"],
                "recommended_change": review["suggested_action"],
            }
            state["decision_points"].append(decision)
            state["decision_points"] = state["decision_points"][-16:]
            state["next_best_action_hint"] = review["suggested_action"] or review["feedback"] or state["next_best_action_hint"]
        return review

    def _phase_from_page(self, page_type: str) -> str:
        return {
            "search_home": "locating_source",
            "search_results": "inspecting_source",
            "dataset_or_resource": "selecting_resource",
            "detail": "inspecting_source",
            "error_or_blocked": "locating_source",
        }.get(page_type, "inspecting_source")

    def _update_task_state_from_observation(self, task: str, observation: dict, sources: list[dict]) -> None:
        state = self._ensure_task_state(task)
        current_url = normalize_url(observation.get("url", ""))
        page_type = observation.get("pageType") or classify_page_from_observation(observation)
        state["active_path"] = current_url
        state["current_page"] = {
            "url": current_url,
            "title": observation.get("title", ""),
            "page_type": page_type,
        }

        if page_type not in {"search_home", "search_results", "login", "error_or_blocked"} and current_url:
            append_unique(
                state["candidate_sources"],
                {
                    "url": current_url,
                    "title": compact_text(observation.get("title", ""), 160),
                    "page_type": page_type,
                    "status": "observed",
                },
                "url",
            )
        for source in sources[-8:]:
            if source.get("usable") is False or source.get("kind") == "search":
                continue
            append_unique(
                state["candidate_sources"],
                {
                    "url": normalize_url(source.get("url", "")),
                    "title": compact_text(source.get("title", ""), 160),
                    "page_type": "observed_source",
                    "status": "observed",
                },
                "url",
            )

        for element in observation.get("elements") or []:
            candidate = resource_candidate_from_element(element, current_url)
            if candidate:
                existing_artifact_names = {
                    str(item.get("name") or "").lower()
                    for item in state["local_artifacts"]
                }
                if candidate["name"].lower() in existing_artifact_names:
                    candidate["status"] = "acquired"
                append_unique(state["candidate_resources"], candidate, "url")

        pending_resources = [
            item for item in state["candidate_resources"]
            if item.get("status") != "acquired"
        ]
        if state["local_artifacts"] and pending_resources:
            state["current_phase"] = "verifying_local_result"
            state["open_questions"] = [
                "Relevant resource candidates remain visible but not locally acquired; decide whether they are required for the requested scope before completing."
            ]
            state["next_best_action_hint"] = (
                "Compare acquired artifacts with remaining candidate resources and either acquire required items "
                "or clearly limit the reported scope."
            )
        elif state["local_artifacts"]:
            state["current_phase"] = "verifying_local_result"
            state["open_questions"] = [
                "Verify that acquired local artifacts match the intended task scope before completing."
            ]
            state["next_best_action_hint"] = "Verify local artifacts and describe any remaining completeness uncertainty."
        else:
            state["current_phase"] = self._phase_from_page(page_type)
            state["open_questions"] = []
            state["next_best_action_hint"] = {
                "locating_source": "Find a relevant public source through visible search or navigation controls.",
                "inspecting_source": "Inspect the source page for credible resource evidence or a better candidate.",
                "selecting_resource": "Compare visible candidate resources and decide which acquisition best matches the task scope.",
            }.get(state["current_phase"], "Continue from current page evidence toward the task goal.")

    def _update_task_state_from_record(self, task: str, record: dict, entry: dict) -> None:
        state = self._ensure_task_state(task)
        action_name = record.get("action", "")
        success = record.get("success")
        action = entry.get("action") or {}
        result = entry.get("result") or {}
        action_reference = {
            "artifact": record.get("artifact"),
            "action_id": record.get("actionId", ""),
            "step": record.get("step", ""),
            "action": action_name,
            "summary": compact_text(record.get("summary") or record.get("result"), 260),
        }
        if self.action_quality_enabled:
            quality_record = {
                "artifact": record.get("artifact"),
            "action_id": record.get("actionId", ""),
                "step": record.get("step", ""),
                "action": action_name,
                "quality": record.get("quality", "neutral"),
                "progress": record.get("progressDelta", "unknown"),
                "evidence": compact_text(record.get("qualityEvidence") or record.get("result"), 260),
                "reason": compact_text(record.get("qualityReason") or record.get("lesson"), 260),
                "retry_recommendation": record.get("retryRecommendation", "inspect_more"),
            }
            state["action_quality"].append(quality_record)
            state["action_quality"] = state["action_quality"][-30:]
            if quality_record["quality"] == "poor" or record.get("avoidRepeat"):
                state["recent_mistakes"].append(quality_record)
                state["recent_mistakes"] = state["recent_mistakes"][-12:]
            elif quality_record["quality"] == "good":
                state["effective_actions"].append(quality_record)
                state["effective_actions"] = state["effective_actions"][-12:]
        if success is True and action_name in {"click", "navigate", "open_tab", "switch_tab", "back", "download", "save_page"}:
            state["completed"].append(action_reference)
            state["completed"] = state["completed"][-20:]
        if success is False or record.get("errorType") or record.get("avoidRepeat"):
            failed_path = {
                **action_reference,
                "url": record.get("url", ""),
                "reason": compact_text(record.get("lesson") or record.get("result"), 260),
                "avoid_repeat": bool(record.get("avoidRepeat", False)),
            }
            append_unique(state["failed_paths"], failed_path, "action_id")
            state["blockers"] = [failed_path["reason"]] if failed_path["reason"] else []
            state["next_best_action_hint"] = "Avoid the recorded failed path and choose a materially different next action."

        if success is True and action_name in {"download", "save_page"}:
            artifact_name = action.get("filename") or str(result.get("path") or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            artifact = {
                "name": artifact_name,
                "path": result.get("path", ""),
                "source_url": result.get("url") or action.get("url", ""),
                "bytes": result.get("bytes", 0),
                "kind": action_name,
                "artifact": record.get("artifact"),
            "action_id": record.get("actionId", ""),
                "verified_local_result": bool(result.get("path") or result.get("bytes")),
            }
            append_unique(state["local_artifacts"], artifact, "name")
            acquired_name = artifact_name.lower()
            acquired_url = normalize_url(artifact["source_url"])
            for candidate in state["candidate_resources"]:
                if (
                    str(candidate.get("name") or "").lower() == acquired_name
                    or normalize_url(candidate.get("url", "")) == acquired_url
                ):
                    candidate["status"] = "acquired"
                    candidate["artifact_name"] = artifact_name
            state["verified_facts"].append(
                {
                    "fact": f"Acquired local artifact: {artifact_name}",
                    "evidence_action_id": record.get("actionId", ""),
                    "source_url": artifact["source_url"],
                }
            )
            state["verified_facts"] = state["verified_facts"][-20:]
            state["current_phase"] = "verifying_local_result"
            state["next_best_action_hint"] = (
                "Check whether acquired local artifacts cover the intended task scope and whether observed peer candidates remain relevant."
            )

    def _derive_run_memory_brief(self, task: str) -> dict:
        state = self._ensure_task_state(task)
        current_page = state.get("current_page") or {}
        artifacts = state.get("local_artifacts") or []
        open_questions = state.get("open_questions") or []
        failures = state.get("failed_paths") or []
        candidate_resources = state.get("candidate_resources") or []
        candidate_sources = state.get("candidate_sources") or []
        return {
            "task_summary": compact_text(task, 260),
            "current_position": {
                "phase": state.get("current_phase", ""),
                "page_type": current_page.get("page_type", ""),
                "title": compact_text(current_page.get("title", ""), 160),
                "url": current_page.get("url", ""),
            },
            "progress_summary": (
                f"completed_actions={len(state.get('completed') or [])}; "
                f"local_artifacts={len(artifacts)}; failed_paths={len(failures)}; "
                f"candidate_resources={len(candidate_resources)}; open_questions={len(open_questions)}"
            ),
            "successful_path": (state.get("effective_actions") or state.get("completed") or [])[-6:],
            "failed_paths": failures[-6:],
            "known_candidates": [
                *candidate_resources[-8:],
                *candidate_sources[-4:],
            ][:10],
            "verified_outputs": artifacts[-8:],
            "decision_points": (state.get("decision_points") or [])[-6:],
            "recent_mistakes": (state.get("recent_mistakes") or [])[-6:],
            "recommended_next_step": state.get("next_best_action_hint", ""),
        }

    def _derive_task_memory(
        self,
        task: str,
        observation: dict,
        last_result: dict | None,
        recent_records: list[dict],
        recalled_records: list[dict],
        lessons: list[dict],
    ) -> dict:
        successful = [record for record in self.raw_archive if record.get("success") is True]
        important_facts = []
        for record in [*recalled_records[-4:], *recent_records[-6:]]:
            text = compact_text(record.get("summary") or record.get("result") or record.get("snippet"), 260)
            if text and text not in important_facts:
                important_facts.append(text)

        current_url = normalize_url(observation.get("url", ""))
        page_type = observation.get("pageType") or classify_page_from_observation(observation)
        persistent_state = json.loads(json.dumps(self._ensure_task_state(task), ensure_ascii=False))
        return {
            **persistent_state,
            "completed_recent": [
                {
                    "action_id": record.get("actionId"),
                    "step": record.get("step"),
                    "summary": compact_text(record.get("summary"), 220),
                }
                for record in successful[-6:]
            ],
            "pending_hint": persistent_state.get("next_best_action_hint")
            or "Continue from the current page toward the user goal; use recalled failures to avoid loops.",
            "important_facts": important_facts[:10],
            "current_page": {
                "url": current_url,
                "title": observation.get("title", ""),
                "page_type": page_type,
            },
            "last_result": last_result,
            "lessons": lessons,
        }

