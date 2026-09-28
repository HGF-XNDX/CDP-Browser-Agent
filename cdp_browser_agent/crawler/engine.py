from __future__ import annotations

import asyncio
from copy import deepcopy
import csv
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

from protego import Protego

from ..harness.tools import Tool
from ..processing.engine import cell
from ..web.tools import WebTools
from ..web.network import WebError
from ..workflows.store import atomic_json
from .spec import CRAWL_SCHEMA, canonical, digest, in_scope, obj, origin, parse_page, prepare
from .store import CrawlBusy, CrawlPaused, CrawlStore


DEFAULTS = {"enabled": True, "state_dir": "downloads/crawls", "max_pages": 200, "max_records": 5000,
    "request_delay_seconds": 0.5, "max_retries": 2, "run_timeout_seconds": 30,
    "respect_robots": True, "max_data_bytes": 50_000_000}
USER_AGENT = "CDP-Browser-Agent"


def retry_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 5.0
    return max(0, delay) if math.isfinite(delay) else 5.0


class Crawler:
    def __init__(self, config, *, parent_id=None, allowed_origins=None):
        self.config = config
        self.settings = {**DEFAULTS, **config.get("crawler", {})}
        self.root = Path(self.settings["state_dir"]).resolve()
        self.parent_id = parent_id
        self.allowed_origins = {origin(u) for u in allowed_origins} if allowed_origins is not None else None
        for key, lo, hi in (("max_pages", 1, 500), ("max_records", 1, 10000), ("max_retries", 0, 3), ("max_data_bytes", 1000, 100_000_000)):
            value = self.settings[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError("Invalid crawler setting: " + key)
        for key, lo, hi in (("request_delay_seconds", 0, 60), ("run_timeout_seconds", .1, 3600)):
            value = self.settings[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError("Invalid crawler setting: " + key)
        if not all(isinstance(self.settings[k], bool) for k in ("enabled", "respect_robots")):
            raise ValueError("Crawler enabled/respect_robots must be booleans")

    def _scope(self, state):
        if self.allowed_origins is not None and not {origin(u) for u in state["spec"]["seed_urls"]} <= self.allowed_origins:
            raise ValueError("Crawl seeds are outside the enclosing workflow origins")

    def create(self, spec):
        if not self.settings["enabled"] or not self.config.get("web", {}).get("enabled", True):
            raise ValueError("HTTP crawling is disabled by the operator")
        normalized = prepare(spec, self.settings)
        self._scope({"spec": normalized})
        with CrawlStore(self.root) as store:
            return self.summary(store.create(normalized, self.parent_id))

    @staticmethod
    def summary(state):
        failures = [p for p in state["queue"] if p["status"] in {"failed", "needs_browser", "blocked"}]
        pending = [p for p in state["queue"] if p["status"] in {"pending", "fetching"}]
        fallback = [{"url": p["url"], "reason": p.get("error")} for p in failures if p["status"] == "needs_browser"]
        return {"ok": state["status"] == "completed", "crawl_id": state["crawl_id"], "parent_run_id": state["parent_run_id"],
            "status": state["status"], "reason": state.get("reason"), "active": state.get("active", False),
            "spec_hash": state["spec_hash"], "record_count": len(state["records"]), "duplicate_records": state["duplicates"],
            "pages_finished": len(state["queue"])-len(pending), "pending_pages": len(pending), "failed_pages": len(failures),
            "request_attempts": sum(p["attempts"] for p in state["queue"]), "coverage": "bounded",
            "scope_completed": state["status"] == "completed", "browser_fallback": fallback[:20], "needs_browser": bool(fallback),
            "failures": [{"url": p["url"], "status": p["status"], "reason": p.get("error")} for p in failures[:20]],
            "next_retry_at": min((p["due"] for p in pending), default=None),
            "artifact_paths": state.get("artifact_paths", []), "dataset": state.get("dataset"),
            "model_calls": 0, "browser_started": False}

    def status(self, identity):
        with CrawlStore(self.root) as store:
            state = store.get(identity, self.parent_id)
            self._scope(state)
            return self.summary(state)

    def list(self):
        if not (self.root / "crawls.sqlite3").exists():
            return []
        with CrawlStore(self.root) as store:
            return [self.summary(s) for s in store.list(self.parent_id)]

    def pause(self, identity):
        with CrawlStore(self.root) as store:
            state = store.get(identity, self.parent_id)
            self._scope(state)
            store.pause(identity, self.parent_id)
            return {**self.summary(state), "pause_requested": True}

    def read(self, identity, offset=0, limit=4000):
        with CrawlStore(self.root) as store:
            state = store.get(identity, self.parent_id)
            self._scope(state)
            if not state.get("dataset"):
                return {"ok": False, "crawl_id": identity, "message": "No committed dataset export yet; inspect crawl status"}
            return {**store.artifacts(identity).read(state["dataset"]["artifact_id"], offset, limit), "crawl_id": identity}

    def records(self, identity, *, require_complete=True):
        with CrawlStore(self.root) as store:
            state = store.get(identity, self.parent_id)
            self._scope(state)
            if require_complete and (state["status"] != "completed" or state["active"]):
                raise ValueError("Crawl is incomplete; finish it before delegating its dataset")
            return store.records(state)

    def definitions(self):
        identity = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
        return [
            Tool("web_crawl", "Batch HTTP crawl without per-page model calls or a browser. Supply a bounded spec OR resume with crawl_id. CSS extraction and static link pagination supported. Reuse the ID after paused; inspect failures and browser_fallback. Feed completed data to delegate_processing(profile,crawl_id).",
                 obj({"spec": CRAWL_SCHEMA, "crawl_id": identity, "page_budget": {"type": "integer", "minimum": 1, "maximum": 50}}), self.run),
            Tool("web_crawl_status", "Read crawl progress, scope coverage and fallback pages.", obj({"crawl_id": identity}, ["crawl_id"]), self.status_async, read_only=True),
            Tool("web_crawl_read", "Read hash-checked collected records as JSON text by character offsets; limits do not truncate stored evidence.", obj({"crawl_id": identity, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, ["crawl_id"]), self.read_async, read_only=True),
            Tool("web_crawl_pause", "Request a running crawl to pause; retain completed pages and its pending frontier.", obj({"crawl_id": identity}, ["crawl_id"]), self.pause_async)
        ]

    async def status_async(self, crawl_id):
        return self.status(crawl_id)

    async def read_async(self, crawl_id, offset=0, limit=4000):
        return self.read(crawl_id, offset, limit)

    async def pause_async(self, crawl_id):
        return self.pause(crawl_id)

    def _export(self, store, state):
        rows = store.records(state)
        dataset = store.artifacts(state["crawl_id"]).save(rows)
        dataset["retrieval"] = "web_crawl_read(crawl_id, offset, limit)"
        folder = self.root / state["crawl_id"] / "exports" / uuid4().hex
        folder.mkdir(parents=True)
        atomic_json(folder / "records.json", rows)
        (folder / "records.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in rows), encoding="utf-8")
        columns = list(dict.fromkeys(k for r in rows for k in r["data"])) + ["_source_url", "_record_key"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: cell(v) for k, v in {**row["data"], "_source_url": row["source_url"], "_record_key": row["record_key"]}.items()})
        (folder / "records.csv").write_bytes(buffer.getvalue().encode("utf-8-sig"))
        atomic_json(folder / "pages.json", state["queue"])
        state.update(dataset=dataset, artifact_paths=[str(folder / name) for name in ("records.json", "records.jsonl", "records.csv", "pages.json")])
        atomic_json(folder / "manifest.json", {"spec": state["spec"], "spec_hash": state["spec_hash"], "dataset": dataset,
            "files": {Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in state["artifact_paths"]}})
        state["artifact_paths"].append(str(folder / "manifest.json"))

    async def run(self, spec=None, crawl_id=None, page_budget=10):
        if not self.settings["enabled"] or not self.config.get("web", {}).get("enabled", True):
            raise ValueError("HTTP crawling is disabled by the operator")
        if (spec is None) == (crawl_id is None):
            raise ValueError("Supply exactly one of spec or crawl_id")
        if isinstance(page_budget, bool) or not isinstance(page_budget, int) or not 1 <= page_budget <= 50:
            raise ValueError("page_budget must be 1..50")
        identity = crawl_id or self.create(spec)["crawl_id"]
        with CrawlStore(self.root) as store:
            existing = store.get(identity, self.parent_id)
            self._scope(existing)
            prepare(existing["spec"], self.settings)  # Recheck tightened operator limits on resume.
            try:
                state, admitted = store.acquire(identity, self.parent_id)
            except CrawlBusy:
                return {**self.status(identity), "ok": False, "status": "busy"}
            if not admitted:
                return self.summary(state)
            operation = asyncio.create_task(self._execute(store, state, page_budget))
            interrupted = None
            try:
                async def monitor():
                    while not operation.done():
                        await asyncio.wait({operation}, timeout=.25)
                        store.check(identity)
                    await operation
                await asyncio.wait_for(monitor(), self.settings["run_timeout_seconds"])
            except BaseException as exc:
                operation.cancel()
                try:
                    await operation
                except BaseException:
                    pass
                for page in state["queue"]:
                    if page["status"] == "fetching":
                        page["status"] = "pending"
                state.update(status="paused" if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError, CrawlPaused)) else "failed",
                    reason="interrupted" if isinstance(exc, asyncio.CancelledError) else "deadline" if isinstance(exc, asyncio.TimeoutError) else str(exc)[:500])
                if isinstance(exc, asyncio.CancelledError):
                    interrupted = exc
            self._export(store, state)
            store.save(state, finish=True)
            if interrupted:
                raise interrupted
            return self.summary(store.get(identity, self.parent_id))

    async def _execute(self, store, state, page_budget):
        spec = state["spec"]
        web_config = deepcopy(self.config.get("web", {}))
        web_config["artifact_dir"] = str(self.root / state["crawl_id"] / "evidence")
        web = WebTools(web_config, max_result_chars=24000, artifact_root=web_config["artifact_dir"])
        web.allowed_origins = spec["seed_urls"]
        delay = self.settings["request_delay_seconds"]
        parsed_robots = {}

        async def throttle(site, interval):
            while True:
                store.check(state["crawl_id"])
                wait = store.reserve_request(site, interval)
                if wait <= 0:
                    return
                await asyncio.sleep(min(wait, .5))

        async def guard(url):
            url = canonical(url)
            if not in_scope(url, spec):
                raise WebError("restricted_url", "Redirect or URL is outside crawl scope")
            site = origin(url)
            wait = delay
            if self.settings["respect_robots"]:
                saved = state["robots"].get(site)
                if not saved or time.time()-saved["checked"] >= 86400:
                    parts = urlsplit(url)
                    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
                    await throttle(site, delay)
                    response = await web._request(robots_url, purpose="fetch", attempts=[], public_only=True,
                        allowed_private_hosts=web.settings.get("allowed_private_hosts", []), allowed_origins=[url])
                    code = response["status_code"]
                    if code == 429 or code >= 500:
                        store.cooldown(site, retry_delay(response["headers"].get("retry-after")))
                        raise WebError("robots_unavailable", "robots.txt temporarily unavailable; retry later")
                    if code not in {200, 404, 410, 401, 403}:
                        raise WebError("robots_unavailable", "robots.txt could not be verified")
                    body = response["body"].decode("utf-8", errors="replace") if code == 200 else "User-agent: *\nDisallow: /" if code in {401, 403} else ""
                    saved = {"body": body, "checked": time.time(), "http_status": code}
                    state["robots"][site] = saved
                    store.save(state)
                if site not in parsed_robots:
                    parsed_robots[site] = Protego.parse(saved["body"])
                parser = parsed_robots[site]
                if not parser.can_fetch(url, USER_AGENT):
                    raise WebError("robots_disallowed", "robots.txt disallows this URL")
                rate = parser.request_rate(USER_AGENT)
                wait = max(wait, parser.crawl_delay(USER_AGENT) or 0, rate.seconds/rate.requests if rate and rate.requests > 0 else 0)
            await throttle(site, wait)
            store.check(state["crawl_id"])
        web.request_guard = guard
        processed = 0
        while True:
            store.check(state["crawl_id"])
            pending = [p for p in state["queue"] if p["status"] == "pending"]
            if not pending or state["limited"]:
                failed = any(p["status"] in {"failed", "needs_browser", "blocked"} for p in state["queue"])
                state.update(status="incomplete" if failed or state["limited"] else "completed",
                    reason="limit_reached" if state["limited"] else "failed_sources" if failed else "frontier_exhausted_within_scope")
                return
            if processed >= page_budget:
                state.update(status="paused", reason="page_budget")
                return
            finished = len(state["queue"])-len(pending)
            if finished >= spec["max_pages"]:
                state.update(limited=True)
                continue
            page = min(pending, key=lambda p: (p["depth"], p["due"]))
            if page["due"] > time.time():
                state.update(status="paused", reason="retry_backoff")
                return
            page.update(status="fetching", attempts=page["attempts"]+1)
            store.save(state)
            result = await web.fetch(page["url"])
            store.check(state["crawl_id"])
            processed += 1
            # Persist every fetch outcome, including failed attempts, before retry.
            page.setdefault("receipts", []).append(store.artifacts(state["crawl_id"]).save(result))
            temporary = result.get("status") in {"network_error", "timeout", "robots_unavailable", "rate_limited"} or result.get("http_status", 0) >= 500
            if not result["ok"]:
                pause = retry_delay(result.get("retry_after")) if result.get("retry_after") or result.get("status") == "rate_limited" else 2 ** min(page["attempts"], 10)
                if result.get("status") == "rate_limited" or result.get("retry_after"):
                    store.cooldown(origin(page["url"]), pause)
                page.update(error=result.get("status", "fetch_failed"), needs_browser=result.get("needs_browser", False))
                if temporary and page["attempts"] <= self.settings["max_retries"]:
                    page.update(status="pending", due=time.time()+pause)
                else:
                    page["status"] = "needs_browser" if result.get("needs_browser") and not temporary else "blocked" if result.get("status") in {"robots_disallowed", "restricted_url", "rate_limited", "robots_unavailable"} else "failed"
                store.save(state)
                continue
            try:
                final_url = canonical(result["url"])
                if any(p is not page and p.get("final_url") == final_url and p["status"] == "done" for p in state["queue"]):
                    page.update(status="duplicate", final_url=final_url)
                    store.save(state)
                    continue
                paths = [Path(p) for p in result["artifact_paths"]]
                raw, text = paths[1].read_bytes(), paths[0].read_text(encoding="utf-8")
                if hashlib.sha256(raw).hexdigest() != result["response_sha256"] or hashlib.sha256(text.encode()).hexdigest() != result["text_sha256"]:
                    raise ValueError("Fetched evidence hash changed")
                rows, links = parse_page(raw, result["content_type"], final_url, text, result["title"], spec, page["depth"])
                keys, added, duplicates = dict(state["keys"]), [], 0
                data_bytes = state.get("data_bytes", 0)
                for data in rows:
                    if spec.get("key_fields") and any(data.get(k) in (None, "") for k in spec["key_fields"]):
                        raise ValueError("A crawl record key field is empty")
                    key = digest({k: data[k] for k in spec["key_fields"]}) if spec.get("key_fields") else digest({"url": final_url, "data": data})
                    if key in keys:
                        if keys[key] != digest(data):
                            raise ValueError("Conflicting records share the same key_fields")
                        duplicates += 1
                        continue
                    size = len(json.dumps(data, ensure_ascii=False).encode())
                    if len(state["records"])+len(added) >= spec["max_records"] or data_bytes+size > self.settings["max_data_bytes"]:
                        state["limited"] = True
                        break
                    record = {"record_key": key, "source_url": final_url, "requested_url": page["url"], "data": data,
                        "accessed_at": result["accessed_at"], "response_sha256": result["response_sha256"],
                        "text_sha256": result["text_sha256"], "artifact_paths": result["artifact_paths"]}
                    added.append(store.artifacts(state["crawl_id"]).save(record))
                    keys[key] = digest(data)
                    data_bytes += size
                state["records"].extend(added)
                state.update(keys=keys, data_bytes=data_bytes, duplicates=state["duplicates"]+duplicates)
                known = {p["url"]: p for p in state["queue"]}
                for link in links:
                    if link["url"] in known:
                        known[link["url"]]["depth"] = min(known[link["url"]]["depth"], link["depth"])
                        continue
                    if len(state["queue"]) >= 5000:
                        state["limited"] = True
                        break
                    item = {**link, "status": "pending", "attempts": 0, "due": 0}
                    state["queue"].append(item)
                    known[link["url"]] = item
                page.update(status="done", final_url=final_url, records_seen=len(rows))
            except ValueError as exc:
                page.update(status="failed", error=str(exc)[:500])
            store.save(state)
