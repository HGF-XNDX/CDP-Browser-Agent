from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time
import json

from .context_budget import ContextBudget, ContextWindowExceeded, route_key

import httpx


DEFAULT_BASE_URL = "http://localhost:8080/v1"
RUN_METRICS = ContextVar("run_metrics", default=None)

# Network-level transient errors that justify automatic retry with backoff.
# 5xx server errors, connection drops, and read timeouts all qualify.
_RETRYABLE_STATUS = {500, 502, 503, 504, 408, 429}


def extract_assistant_content(data: dict) -> str:
    if isinstance(data.get("output"), list):
        return "".join(part.get("text", "") for item in data["output"]
                      if item.get("type") == "message" and item.get("role", "assistant") == "assistant"
                      for part in item.get("content", [])
                      if part.get("type") == "output_text" and isinstance(part.get("text"), str))
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    for value in (message.get("content"), message.get("reasoning_content"), choice.get("text")):
        if isinstance(value, str) and value.strip():
            return value
    return ""


_capability_cache: dict[str, tuple[float, dict]] = {}


def _positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _request_headers(options: dict) -> httpx.Headers:
    extra = options.get("extraHeaders") or {}
    if not isinstance(extra, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in extra.items()):
        raise ValueError("model.extraHeaders must map header names to string values")
    headers = httpx.Headers({"Content-Type": "application/json"})
    headers.update(extra)
    if options.get("apiKey"):
        headers["Authorization"] = f"Bearer {options['apiKey']}"
    return headers


def _responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    instructions, items = [], []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role in {"system", "developer"} and isinstance(content, str):
            instructions.append(content)
            continue
        if role not in {"user", "assistant"}:
            raise ValueError("Responses supports text system/developer instructions and user/assistant messages")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise ValueError("Responses message content must be text or a supported content list")
        parts = []
        for part in content:
            if part.get("type") == "text":
                parts.append({"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]})
            elif part.get("type") == "image_url" and role == "user":
                image = part["image_url"]
                parts.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
            else:
                raise ValueError("Unsupported Responses message content type")
        items.append({"type": "message", "role": role, "content": parts})
    return "\n\n".join(instructions), items


async def prepare_model_options(options: dict) -> dict:
    """Discover active capacity, not training capacity. Failures have a short TTL."""
    result = dict(options)
    requested = options.get("_requested_model", options.get("model") or "")
    key = route_key({**options, "model": requested})
    cached = _capability_cache.get(key)
    if cached and cached[0] > time.monotonic():
        capability = dict(cached[1])
    else:
        base = (options.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
        provider = options.get("provider") or "llama.cpp"
        capability = {"model": requested or ("deepseek-chat" if provider == "deepseek" else options.get("fallbackModel", "local-model")),
                      "context_window_tokens": None, "source": "fallback_32768"}
        headers = _request_headers(options)
        seconds = max(.1, float(options.get("modelDiscoveryTimeout", 5)))
        async with httpx.AsyncClient(timeout=seconds, trust_env=bool(options.get("trustEnv", False))) as client:
            # Only llama.cpp has the /props contract. It is responsive even while
            # the sole inference slot is occupied. Match explicitly named models.
            if provider == "llama.cpp":
                root = base[:-3] if base.endswith("/v1") else base
                try:
                    response = await client.get(root + "/props", headers=headers)
                    response.raise_for_status()
                    props = response.json()
                    alias = props.get("model_alias") or props.get("model_path")
                    if alias and (not requested or requested in {alias, props.get("model_path")}):
                        capability.update(model=requested or alias,
                            context_window_tokens=_positive_int((props.get("default_generation_settings") or {}).get("n_ctx")),
                            source="llama.cpp/props", slots=_positive_int(props.get("total_slots")))
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    pass
            if capability["context_window_tokens"] is None:
                try:
                    response = await client.get(base + "/models", headers=headers)
                    response.raise_for_status()
                    values = response.json().get("data") or []
                    selected = next((v for v in values if isinstance(v, dict) and v.get("id") == requested), {}) if requested else (values[0] if values else {})
                    if selected.get("id"):
                        meta = selected.get("meta") or {}
                        # n_ctx_train is deliberately ignored: it is not the
                        # runtime allocation and can exceed the server slot.
                        capacity = _positive_int(meta.get("n_ctx")) or _positive_int(selected.get("context_window"))
                        capability.update(model=selected["id"], context_window_tokens=capacity,
                                          source="models" if capacity else "fallback_32768")
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    pass
        ttl = 60 if capability["context_window_tokens"] else 5
        _capability_cache[key] = (time.monotonic() + ttl, dict(capability))
    result["_requested_model"] = requested
    result["model"] = capability["model"]
    result["_model_capabilities"] = capability
    # Resolved and auto-discovered routes refer to the same server/model.
    _capability_cache[route_key(result)] = _capability_cache[key]
    return result


async def resolve_model_name(base_url: str, model_override: str, options: dict) -> str:
    return (await prepare_model_options({**options, "baseUrl": base_url, "model": model_override}))["model"]


def _raise_context_overflow(response):
    if response.status_code not in {400, 413, 422}:
        return
    try:
        data = response.json()
        error = data.get("error") or data
        code = str(error.get("code") or error.get("type") or "").lower()
        message = str(error.get("message") or "").lower()
    except (ValueError, AttributeError, TypeError):
        return
    codes = {"context_length_exceeded", "context_window_exceeded", "exceed_context_size_error", "n_ctx_exceeded"}
    if code in codes or any(term in message for term in ("exceeds the available context size", "maximum context length", "exceed_context_size", "context window is full")):
        raise ContextWindowExceeded("Provider rejected context length; compact the prompt before retrying")


async def chat_completion(messages: list[dict], options: dict | None = None) -> str:
    started = time.monotonic()
    options = await prepare_model_options(options or {})
    provider = options.get("provider") or "llama.cpp"
    enable_thinking = bool(options.get("enableThinking", False))
    base_url = (options.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    headers = _request_headers(options)
    endpoint = "/" + str(options.get("apiEndpoint") or "chat/completions").strip("/")
    if endpoint not in {"/chat/completions", "/responses"}:
        raise ValueError("model.apiEndpoint must be /chat/completions or /responses")
    payload = {
        "model": options["model"],
        "messages": messages,
        "max_tokens": options.get("maxTokens", 2048),
        "stream": False,
    }
    if options.get("temperature", 0.1) is not None:
        payload["temperature"] = options.get("temperature", 0.1)
    budget_messages = messages
    if endpoint == "/responses":
        instructions, items = _responses_input(messages)
        payload = {"model": options["model"], "instructions": instructions, "input": items,
                   "max_output_tokens": options.get("maxTokens", 2048), "stream": False,
                   "store": False, "tool_choice": "none"}
        if options.get("temperature", 0.1) is not None:
            payload["temperature"] = options.get("temperature", 0.1)
        if not enable_thinking:
            payload["text"] = {"format": {"type": "json_object"}}
            if not any('json' in part.get('text', '').lower() for item in items for part in item['content']):
                cue = "Return valid JSON."
                user = next((item for item in reversed(items) if item['role'] == 'user'), None)
                if user is None:
                    items.append({'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': cue}]})
                else:
                    user['content'].append({'type': 'input_text', 'text': cue})
                # Native adapters can require the JSON cue in input, not just
                # instructions. Include it in admission without altering source data.
                budget_messages = [*messages, {'role': 'user', 'content': cue}]
    else:
        if not enable_thinking:
            payload["response_format"] = {"type": "json_object"}
        if provider == "llama.cpp":
            payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    ContextBudget.from_settings(options, options.get("_agent_context"), RUN_METRICS.get()).check(budget_messages)
    seconds = max(10.0, float(options.get("apiTimeout", 180)))
    timeout = httpx.Timeout(seconds, connect=min(20, seconds), read=seconds, write=seconds, pool=20)
    max_retries = max(0, int(options.get("maxRetries", 3)))
    base_backoff = float(options.get("retryBackoffSeconds", 1.5))

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout, trust_env=bool(options.get("trustEnv", False))) as client:
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(f"{base_url}{endpoint}", headers=headers, json=payload)
                _raise_context_overflow(response)
                if response.status_code in {400, 422} and "response_format" in payload:
                    payload.pop("response_format", None)
                    response = await client.post(f"{base_url}{endpoint}", headers=headers, json=payload)
                elif response.status_code in {400, 422} and endpoint == "/responses" and "text" in payload:
                    payload.pop("text")
                    response = await client.post(f"{base_url}{endpoint}", headers=headers, json=payload)
                _raise_context_overflow(response)
                if response.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                    await asyncio.sleep(base_backoff * (2 ** attempt))
                    continue
                response.raise_for_status()
                break
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(base_backoff * (2 ** attempt))
                    continue
                raise
        else:
            # Loop exhausted without break — re-raise the last seen exception.
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("chat_completion: retry loop exhausted without response")
    data = response.json()
    metrics = RUN_METRICS.get()
    if metrics is not None:
        usage = data.get("usage") or {}
        metrics["model_calls"] = metrics.get("model_calls", 0) + 1
        metrics["model_seconds"] = round(metrics.get("model_seconds", 0) + time.monotonic()-started, 3)
        input_key, output_key = ("input_tokens", "output_tokens") if endpoint == "/responses" else ("prompt_tokens", "completion_tokens")
        for target, source in (("input_tokens", input_key), ("output_tokens", output_key)):
            metrics[target] = metrics.get(target, 0) + int(usage.get(source) or 0)
        if not usage:
            metrics["usage_missing_calls"] = metrics.get("usage_missing_calls", 0) + 1
        if usage.get(input_key) and all(isinstance(m.get("content"), str) for m in messages):
            route = metrics.setdefault("model_routes", {}).setdefault(route_key(options), {})
            ratio = max(1, min(4, len(json.dumps(messages, ensure_ascii=False)) / usage[input_key]))
            route["observed_chars_per_token"] = min(route.get("observed_chars_per_token", 4), ratio)
            route["prompt_tokens"] = int(usage[input_key])
    if endpoint == "/responses" and data.get("status") in {"failed", "incomplete", "cancelled"}:
        raise RuntimeError("Responses generation did not complete: " + data["status"])
    content = extract_assistant_content(data)
    if not content:
        choice = (data.get("choices") or [{}])[0] or {}
        message = choice.get("message") or {}
        for fallback_key in ("reasoning_content", "reasoning"):
            value = message.get(fallback_key)
            if isinstance(value, str) and value.strip():
                content = value
                break
    if not content:
        raise RuntimeError("Model response did not contain assistant content")
    import re as _re
    content = _re.sub(r"<think>[\s\S]*?</think>\s*", "", content).strip()
    return content
