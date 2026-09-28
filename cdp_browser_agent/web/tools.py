from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4
from urllib.request import proxy_bypass_environment

import httpx

from ..harness.tools import Tool
from .extract import decode, extract_page, parse_search
from .network import WebError, download, ordered_routes, remember_route, valid_url


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


class WebTools:
    def __init__(self, settings=None, max_result_chars=12000, *, artifact_root=None):
        self.settings = deepcopy(settings or {})
        self.max_result_chars = max_result_chars
        self.timeout = float(self.settings.get("timeout_seconds", 30))
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 120:
            raise ValueError("web.timeout_seconds must be between 1 and 120")
        self.max_bytes = int(self.settings.get("max_response_bytes", 2_000_000))
        if not 1024 <= self.max_bytes <= 10_000_000:
            raise ValueError("web.max_response_bytes must be between 1024 and 10000000")
        self.allowed_origins = None  # Bound by an enclosing workflow, when applicable.
        self.request_guard = None  # Host-owned crawler scope/robots/rate guard, including redirects.
        self.cache = {}
        self.artifacts = Path(self.settings.get("artifact_dir", "downloads/web")).expanduser().resolve() / uuid4().hex
        if artifact_root is not None:
            self.artifacts = Path(artifact_root).resolve()  # Caller already owns a unique run directory.

    def definitions(self):
        return [Tool("web_search", "Quick web discovery without a browser. Results are snippets, not full pages. Fetch relevant URLs; use browser_url if search is blocked.",
                     schema({"query": {"type": "string", "minLength": 1, "maxLength": 500},
                             "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]), self.search),
                Tool("web_fetch", "Read a public HTTP(S) page without a browser/cookies. content_format='html' returns bounded raw HTML for inspecting CSS selectors before crawling; default is extracted text. Use offset for more. needs_browser indicates dynamic/auth/challenge content.",
                     schema({"url": {"type": "string", "minLength": 1, "maxLength": 8000},
                             "offset": {"type": "integer", "minimum": 0},
                             "max_chars": {"type": "integer", "minimum": 500, "maximum": 12000},
                             "content_format": {"enum": ["text", "html"]}}, ["url"]), self.fetch)]

    async def _request(self, url, *, purpose, attempts, **kwargs):
        mode = self.settings.get(purpose, {}).get("proxy", self.settings.get("proxy", "auto"))
        if mode is not None and not isinstance(mode, str):
            raise WebError("configuration_error", "Proxy mode must be auto, an explicit URL, or an empty string for direct")
        if mode == "auto" and proxy_bypass_environment(valid_url(url).host):
            mode = ""
        for route in ordered_routes(mode, purpose):
            try:
                response = await download(url, proxy=route[1], max_bytes=self.max_bytes,
                    timeout=min(12, self.timeout), **kwargs)
            except (httpx.HTTPError, OSError, asyncio.TimeoutError, ImportError, ValueError) as exc:
                if isinstance(exc, WebError):
                    raise
                # Do not expose exception strings: proxy URIs may include secrets.
                attempts.append({"route": route[0], "error_type": type(exc).__name__})
                continue
            attempts.append({"route": route[0], "http_status": response["status_code"]})
            if 200 <= response["status_code"] < 300:
                remember_route(mode, purpose, route)
            # HTTP challenges/rate limits are not a reason to rotate proxy routes.
            response["network_route"] = route[0]
            return response
        raise WebError("network_error", "All configured network routes failed", needs_browser=True)

    def _fit(self, value):
        while len(json.dumps(value, ensure_ascii=False)) > self.max_result_chars - 100:
            if len(value.get("text", "")) > 100:
                value["text"] = value["text"][:max(100, len(value["text"]) - 1000)]
                value["truncated"] = True
                value["next_offset"] = value.get("offset", 0) + len(value["text"])
            elif value.get("links"):
                value["links"].pop()
            elif "requested_url" in value:
                value.pop("requested_url")
            elif "redirects" in value:
                value.pop("redirects")
            elif "browser_url" in value and value.get("browser_url") == value.get("url"):
                value.pop("browser_url", None)
            elif len(value.get("results", [])) > 1:
                value["results"].pop()
                value["truncated"] = True
            elif value.get("network_attempts"):
                value["network_attempts"].pop(0)
            else:
                break
        return value

    async def search(self, query, max_results=5):
        attempts = []
        result = {"query": query, "searched_at": timestamp(), "network_attempts": attempts,
                  "browser_url": "https://www.bing.com/search?" + urlencode({"q": query})}
        try:
            if not isinstance(query, str) or not query.strip() or len(query) > 500 or isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 10:
                raise WebError("invalid_arguments", "query must be nonempty (<=500 characters) and max_results must be 1..10")
            if not self.settings.get("enabled", True):
                raise WebError("disabled", "Web tools are disabled")
            async def execute():
                search = self.settings.get("search", {})
                provider = search.get("provider", "auto")
                providers = ["bing_rss", "duckduckgo"] if provider == "auto" else [provider]
                errors = []
                for provider in providers:
                    result["provider"] = provider
                    try:
                        kwargs = {"public_only": False}
                        if provider == "bing_rss":
                            url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})
                        elif provider == "duckduckgo":
                            url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
                        elif provider == "searxng":
                            endpoint = search.get("endpoint")
                            if not endpoint:
                                raise WebError("configuration_error", "Configure web.search.endpoint for your SearXNG /search endpoint")
                            url = str(valid_url(endpoint)) + ("&" if "?" in endpoint else "?") + urlencode({"q": query, "format": "json"})
                        elif provider == "tavily":
                            key = os.environ.get(search.get("api_key_env", "TAVILY_API_KEY"))
                            if not key:
                                raise WebError("configuration_error", "Configured search API key environment variable is missing")
                            url = "https://api.tavily.com/search"
                            kwargs.update(method="POST", headers={"Authorization": "Bearer " + key}, json_body={
                                "query": query, "max_results": max_results, "search_depth": "basic", "include_answer": False, "include_raw_content": False})
                        else:
                            raise WebError("configuration_error", "Supported search providers: auto, bing_rss, duckduckgo, searxng, tavily")
                        response = await self._request(url, purpose="search", attempts=attempts, **kwargs)
                        code = response["status_code"]
                        if not 200 <= code < 300:
                            raise WebError("rate_limited" if code == 429 else "provider_error", f"Search HTTP {code}", needs_browser=code != 429)
                        if provider in {"tavily", "searxng"}:
                            try:
                                payload = json.loads(response["body"])
                            except (ValueError, UnicodeDecodeError):
                                raise WebError("parse_error", "Search API returned invalid JSON", needs_browser=True) from None
                            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list) or not all(isinstance(r, dict) for r in payload["results"]):
                                raise WebError("parse_error", "Search API returned no results array", needs_browser=True)
                            rows = [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")} for r in payload["results"]]
                        else:
                            rows = parse_search(provider, response["body"])
                        clean, seen = [], set()
                        for row in rows:
                            try:
                                target = str(valid_url(row["url"]))
                            except WebError:
                                continue
                            if target not in seen:
                                clean.append({"title": str(row["title"])[:300], "url": target,
                                              "snippet": str(row["snippet"])[:600]})
                                seen.add(target)
                        if rows and not clean:
                            raise WebError("parse_error", "Search results had no usable HTTP(S) URLs", needs_browser=True)
                        return {**result, "ok": True, "status": "success" if clean else "empty", "results": clean[:max_results],
                                "needs_browser": False, "network_route": response["network_route"], "provider_attempts": errors}
                    except WebError as exc:
                        errors.append({"provider": provider, "status": exc.status})
                        if len(providers) == 1 or provider == providers[-1]:
                            result["provider_attempts"] = errors
                            raise
            return self._fit(await asyncio.wait_for(execute(), self.timeout))
        except asyncio.TimeoutError:
            return {**result, "ok": False, "status": "timeout", "needs_browser": True, "results": [], "message": "Search deadline reached"}
        except WebError as exc:
            return {**result, "ok": False, "status": exc.status, "needs_browser": exc.needs_browser, "results": [], "message": str(exc)}

    async def fetch(self, url, offset=0, max_chars=6000, content_format="text"):
        attempts = []
        result = {"requested_url": url, "network_attempts": attempts}
        try:
            current = str(valid_url(url))
            if content_format not in {"text", "html"}:
                raise WebError("invalid_arguments", "content_format must be text or html")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 500 <= max_chars <= 12000:
                raise WebError("invalid_arguments", "offset must be nonnegative and max_chars must be 500..12000")
            if not self.settings.get("enabled", True):
                raise WebError("disabled", "Web tools are disabled")
            async def execute():
                cached = self.cache.get(current)
                if offset > 0 and cached and time.monotonic() - cached[1] < 60:
                    return deepcopy(cached[0]), True
                response = await self._request(current, purpose="fetch", attempts=attempts, public_only=True,
                    allowed_private_hosts=self.settings.get("allowed_private_hosts", []), allowed_origins=self.allowed_origins,
                    before_request=self.request_guard)
                code = response["status_code"]
                result.update(url=response["url"], http_status=code, network_route=response["network_route"])
                if response["headers"].get("retry-after"):
                    result["retry_after"] = response["headers"]["retry-after"][:128]
                if not 200 <= code < 300:
                    raise WebError("rate_limited" if code == 429 else "http_error", f"Page returned HTTP {code}", needs_browser=code != 429)
                content_type = response["headers"].get("content-type", "").lower()
                page = await asyncio.to_thread(extract_page, response["body"], content_type, response["url"])
                accessed = timestamp()
                html_sha = hashlib.sha256(response["body"]).hexdigest()
                text_sha = hashlib.sha256(page["text"].encode()).hexdigest()
                directory = self.artifacts / (html_sha + "-" + uuid4().hex[:8])
                directory.mkdir(parents=True, exist_ok=True)
                raw_path, text_path = directory / "response.bin", directory / "content.txt"
                raw_path.write_bytes(response["body"])
                text_path.write_bytes(page["text"].encode("utf-8"))
                metadata = {"url": response["url"], "requested_url": current, "accessed_at": accessed,
                            "content_type": content_type, "response_sha256": html_sha, "text_sha256": text_sha,
                            "extraction": page["extraction"], "redirects": response["redirects"], "http_status": code}
                receipt = directory / "source.json"
                receipt.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                data = {**metadata, **page, "artifact_paths": [str(text_path), str(raw_path), str(receipt)],
                        "network_route": response["network_route"]}
                if len(self.cache) >= 16:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[current] = (deepcopy(data), time.monotonic())
                return data, False
            page, cached = await asyncio.wait_for(execute(), self.timeout)
            content = page["text"]
            if content_format == "html":
                if page["extraction"] == "plain_text":
                    raise WebError("unsupported_content", "HTML inspection requires an HTML response; use content_format=text")
                raw = Path(page["artifact_paths"][1]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != page["response_sha256"]:
                    raise WebError("evidence_changed", "Saved HTML hash differs; fetch the page again")
                content = decode(raw, page["content_type"])
            total = len(content)
            text = content[offset:offset + max_chars]
            return self._fit({**result, **page, "ok": not page["needs_browser"],
                "status": page.get("fallback_reason") or "success", "cache_hit": cached,
                "text": text, "content_format": content_format, "total_chars": total, "offset": offset, "truncated": offset + len(text) < total,
                "next_offset": offset + len(text) if offset + len(text) < total else None,
                "browser_url": page["url"]})
        except asyncio.TimeoutError:
            return {**result, "ok": False, "status": "timeout", "needs_browser": True, "browser_url": url, "message": "Fetch deadline reached"}
        except WebError as exc:
            if exc.status == "invalid_url":
                result.pop("requested_url", None)
            return {**result, "ok": False, "status": exc.status, "needs_browser": exc.needs_browser,
                    **({"browser_url": result.get("url", url)} if exc.needs_browser else {}), "message": str(exc)}


def capabilities(settings):
    settings = settings or {}
    return {"enabled": settings.get("enabled", True), "prefer_fast_path": settings.get("prefer_fast_path", True),
            "search_provider": settings.get("search", {}).get("provider", "auto"),
            "proxy_mode": "auto" if settings.get("proxy", "auto") == "auto" else ("configured" if settings.get("proxy") else "direct"),
            "connections_verified": False}
