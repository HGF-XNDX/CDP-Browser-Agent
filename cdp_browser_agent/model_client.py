from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time
import json

import httpx


DEFAULT_BASE_URL = "http://localhost:8080/v1"
_model_cache: dict[str, str] = {}
RUN_METRICS = ContextVar("run_metrics", default=None)

# Network-level transient errors that justify automatic retry with backoff.
# 5xx server errors, connection drops, and read timeouts all qualify.
_RETRYABLE_STATUS = {500, 502, 503, 504, 408, 429}


def extract_assistant_content(data: dict) -> str:
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    for value in (message.get("content"), message.get("reasoning_content"), choice.get("text")):
        if isinstance(value, str) and value.strip():
            return value
    return ""


async def resolve_model_name(base_url: str, model_override: str, options: dict) -> str:
    if model_override:
        return model_override
    if options.get("provider") == "deepseek":
        return "deepseek-chat"
    key = base_url.rstrip("/")
    if key in _model_cache:
        return _model_cache[key]
    headers = {}
    if options.get("apiKey"):
        headers["Authorization"] = f"Bearer {options['apiKey']}"
    discovery_timeout = max(1.0, float(options.get("modelDiscoveryTimeout", 5)))
    timeout = httpx.Timeout(
        discovery_timeout,
        connect=min(3.0, discovery_timeout),
        read=discovery_timeout,
        write=discovery_timeout,
        pool=discovery_timeout,
    )
    fallback = str(options.get("fallbackModel") or "local-model")
    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=bool(options.get("trustEnv", False)),
    ) as client:
        # llama.cpp exposes its model alias at /props. This endpoint remains
        # responsive while /v1/models can block behind a long generation on a
        # single-slot server, so prefer it for local llama.cpp deployments.
        if (options.get("provider") or "llama.cpp") == "llama.cpp":
            root = key[:-3] if key.endswith("/v1") else key
            try:
                response = await client.get(f"{root}/props", headers=headers)
                response.raise_for_status()
                props = response.json()
                alias = str(
                    props.get("model_alias")
                    or props.get("model_path")
                    or ""
                ).strip()
                if alias:
                    _model_cache[key] = alias
                    return alias
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        try:
            response = await client.get(f"{key}/models", headers=headers)
            response.raise_for_status()
            values = response.json().get("data") or []
            discovered = str((values[0] if values else {}).get("id") or "").strip()
            if discovered:
                _model_cache[key] = discovered
                return discovered
        except (httpx.HTTPError, ValueError, TypeError):
            pass
    # Model discovery is auxiliary. A temporary /models failure must not abort
    # the whole workflow; llama.cpp accepts an arbitrary non-empty model field.
    _model_cache[key] = fallback
    return _model_cache[key]


async def chat_completion(messages: list[dict], options: dict | None = None) -> str:
    started = time.monotonic()
    options = options or {}
    provider = options.get("provider") or "llama.cpp"
    enable_thinking = bool(options.get("enableThinking", False))
    base_url = (options.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    headers = {"Content-Type": "application/json"}
    if options.get("apiKey"):
        headers["Authorization"] = f"Bearer {options['apiKey']}"
    payload = {
        "model": await resolve_model_name(base_url, options.get("model") or "", options),
        "messages": messages,
        "temperature": options.get("temperature", 0.1),
        "max_tokens": options.get("maxTokens", 2048),
        "stream": False,
    }
    if not enable_thinking:
        payload["response_format"] = {"type": "json_object"}
    if provider == "llama.cpp":
        payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    seconds = max(10.0, float(options.get("apiTimeout", 180)))
    timeout = httpx.Timeout(seconds, connect=min(20, seconds), read=seconds, write=seconds, pool=20)
    max_retries = max(0, int(options.get("maxRetries", 3)))
    base_backoff = float(options.get("retryBackoffSeconds", 1.5))

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout, trust_env=bool(options.get("trustEnv", False))) as client:
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                if response.status_code in {400, 422}:
                    payload.pop("response_format", None)
                    response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
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
        for target, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
            metrics[target] = metrics.get(target, 0) + int(usage.get(source) or 0)
        if not usage:
            metrics["usage_missing_calls"] = metrics.get("usage_missing_calls", 0) + 1
        if usage.get("prompt_tokens"):
            metrics["observed_chars_per_token"] = max(1, min(4, len(json.dumps(messages, ensure_ascii=False)) / usage["prompt_tokens"]))
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
