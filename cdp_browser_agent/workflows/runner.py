from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urljoin

from ..browser.agent import run_agent
from ..browser.controller import BrowserController
from ..harness.runtime import ExtensionRuntime
from ..harness.verification import check_page
from .spec import WorkflowCatalog, digest, render, scoped_url
from .store import WorkflowBusy, WorkflowStore, now


class WorkflowStop(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


EXTRACT = """(nodes, args) => ({total: nodes.length, rows: nodes.slice(0,args.limit).map(root => {
    const row = {};
    for (const [name, field] of Object.entries(args.fields)) {
        const el = field.selector ? root.querySelector(field.selector) : root;
        let value = el ? (field.attribute ? el.getAttribute(field.attribute) : (el.innerText ?? el.textContent)) : null;
        if (value != null) value = String(value).replace(/\\r/g,'').trim();
        if (value && value.length > 64000) throw new Error('Field exceeds 64000 characters: '+name);
        row[name] = value;
    }
    return row;
})})"""


class WorkflowExecution:
    def __init__(self, config, store, state, page_budget, retry_uncertain):
        self.config, self.store, self.state = config, store, state
        self.spec = state["spec"]
        self.allowed = self.spec["allowed_origins"]
        self.page_budget = page_budget
        self.retry_uncertain = retry_uncertain
        self.pages_this_attempt = 0
        self.controller = None
        self.runtime = None
        self.output = store.root / state["run_id"]
        self.output.mkdir(exist_ok=True)

    def event(self, event, **data):
        with (self.output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"ts": now(), "attempt": self.state["attempt"], "event": event, **data}, ensure_ascii=False) + "\n")

    def boundary(self):
        if self.store.pause_requested(self.state["run_id"]):
            raise WorkflowStop("paused", "Pause requested; stopped at a checkpoint boundary")
        self.store.save(self.state)

    async def snapshot(self):
        content = (await self.controller.page.content()).encode("utf-8")
        sha = hashlib.sha256(content).hexdigest()
        folder = self.output / "snapshots"
        folder.mkdir(exist_ok=True)
        path = folder / f"{sha}.html"
        if not path.exists():
            path.write_bytes(content)
        return sha

    async def visit(self, url, wait_for=None):
        url = scoped_url(url, self.allowed)
        await asyncio.sleep(self.spec.get("request_delay_ms", 500) / 1000)
        response = await self.controller.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        scoped_url(self.controller.page.url, self.allowed)
        if response and response.status in {401, 403, 429}:
            raise WorkflowStop("needs_input" if response.status != 429 else "blocked", f"HTTP {response.status}: {url}")
        if response and response.status >= 400:
            raise ValueError(f"HTTP {response.status}: {url}")
        if wait_for:
            await self.controller.page.locator(wait_for).first.wait_for(state="visible", timeout=10000)
        return self.controller.page.url

    async def extract(self, selector, fields, limit):
        result = await self.controller.page.locator(selector).evaluate_all(EXTRACT, {"fields": fields, "limit": limit})
        for row in result["rows"]:
            for name, definition in fields.items():
                if not row[name] and definition.get("required", True):
                    raise ValueError(f"Required field missing: {name}; review the workflow selector or page access")
                if row[name] and definition.get("url"):
                    row[name] = urljoin(self.controller.page.url, row[name])
        return result

    async def crawl(self, step):
        sid, run_id = step["id"], self.state["run_id"]
        cursor = self.state["cursors"].get(sid)
        if cursor is None:
            cursor = {"next_url": step.get("start_url") or self.controller.page.url, "pages": 0, "done": False}
            scoped_url(cursor["next_url"], self.allowed)
            self.state["cursors"][sid] = cursor
            self.store.save(self.state)
        while not cursor["done"]:
            self.boundary()
            if self.page_budget is not None and self.pages_this_attempt >= self.page_budget:
                raise WorkflowStop("paused", "Page budget reached; resume this run to continue")
            limit_records = step.get("max_records", 1000)
            remaining = limit_records - self.store.count(run_id, sid)
            if cursor["pages"] >= step.get("max_pages", 20) or remaining <= 0:
                raise WorkflowStop("incomplete", "Configured crawl limit reached with pages remaining")
            url = scoped_url(cursor["next_url"], self.allowed)
            if self.store.seen_page(run_id, sid, url):
                raise WorkflowStop("incomplete", "Pagination cycle detected")
            await self.visit(url, step.get("wait_for"))
            source_url = self.controller.page.url
            if self.store.seen_page(run_id, sid, source_url):
                raise WorkflowStop("incomplete", "Pagination redirected to a previously collected page")
            captured = await self.extract(step["item_selector"], step["fields"], remaining)
            if not captured["total"]:
                empty = step.get("empty_selector")
                if not empty or not await self.controller.page.locator(empty).first.is_visible():
                    raise ValueError("No records and no visible empty_selector; refusing to mark an unknown page as an empty result")
            next_url = None
            if step.get("next_selector"):
                next_link = self.controller.page.locator(step["next_selector"])
                if await next_link.count():
                    if await next_link.count() != 1:
                        raise ValueError("next_selector must identify exactly one link")
                    disabled = await next_link.is_disabled() or await next_link.get_attribute("aria-disabled") == "true"
                    if not disabled:
                        href = await next_link.get_attribute("href")
                        if not href:
                            raise ValueError("Automatic pagination requires an href link; use an agent step for other UI navigation")
                        next_url = scoped_url(urljoin(source_url, href), self.allowed)
            html_hash = await self.snapshot()
            records = []
            for row in captured["rows"]:
                record = {"data": row, "source_url": source_url, "list_url": source_url, "page_sha256": html_hash}
                if step.get("follow"):
                    follow = step["follow"]
                    detail_url = scoped_url(urljoin(source_url, row[follow["url_field"]]), self.allowed)
                    self.boundary()
                    await self.visit(detail_url, follow.get("wait_for"))
                    detail = await self.extract(":root", follow["fields"], 1)
                    record["data"].update(detail["rows"][0])
                    record["source_url"] = self.controller.page.url
                    record["detail_sha256"] = await self.snapshot()
                if any(not record["data"].get(key) for key in step["key_fields"]):
                    raise ValueError("Record key fields must be non-empty")
                records.append(record)
            pages = cursor["pages"] + 1
            limited = captured["total"] > remaining or (bool(next_url) and (pages >= step.get("max_pages", 20) or len(records) >= remaining))
            done = (not next_url and captured["total"] <= remaining) or (limited and step.get("on_limit") == "complete")
            coverage = "bounded" if limited else ("exhausted" if done else "partial")
            cursor = {"next_url": None if done else next_url or url, "pages": pages, "done": done, "coverage": coverage}
            evidence = {"step_id": sid, "url": source_url, "captured_at": now(), "html_sha256": html_hash,
                        "rows_seen": captured["total"], "rows_collected": len(records), "next_url_observed": next_url, "coverage": coverage}
            self.store.commit_page(self.state, step, source_url, records, evidence, cursor)
            self.pages_this_attempt += 1
            self.event("page_committed", **evidence)
            if limited and not done:
                raise WorkflowStop("incomplete", "Configured crawl limit reached; results are partial")
        return {"status": "completed", "pages": cursor["pages"], "records": self.store.count(run_id, sid), "coverage": cursor["coverage"]}

    async def agent_step(self, step):
        previous = self.state["step_results"].get(step["id"], {})
        if previous.get("status") == "running" and not step.get("replay_safe", False) and not self.retry_uncertain:
            raise WorkflowStop("needs_input", "An agent step was interrupted; inspect its effects before explicitly retrying this uncertain step")
        if self.runtime is None:
            model = self.config.setdefault("model", {})
            if model.get("apiKeyEnv"):
                model["apiKey"] = os.environ[model["apiKeyEnv"]]
            self.runtime = await ExtensionRuntime(self.config).__aenter__()
            self.runtime.web.allowed_origins = self.allowed
        for skill in step.get("skills", []):
            self.runtime.skills.load(skill)
        self.state["step_results"][step["id"]] = {"status": "running", "started_at": now()}
        self.store.save(self.state)
        config = deepcopy(self.config)
        config.setdefault("agent", {}).update(max_steps=min(step.get("max_steps", 20), config.get("agent", {}).get("max_steps", 40)),
                                               log_dir=str(self.output / "agent-events"))

        async def verify(controller, state):
            scoped_url(controller.page.url, self.allowed)
            return await check_page(controller, step["checks"])

        def guard(action, observation):
            name = action["action"]
            if name in {"download", "save_page", "open_tab", "switch_tab"}:
                raise ValueError("Workflow agent steps handle page preparation; this action is not enabled")
            if name == "navigate":
                scoped_url(action["url"], self.allowed)
            if action.get("href") and not action["href"].lower().startswith("javascript:"):
                scoped_url(urljoin(self.controller.page.url, action["href"]), self.allowed)
            if name == "tool" and action["name"] not in {*self.runtime.builtin_names, *step.get("allow_tools", [])}:
                raise ValueError("External tool is not allowed by this workflow step")

        task = (step["instructions"] + "\nHost completion checks (do not change them): " + json.dumps(step["checks"], ensure_ascii=False)
                + "\nThis workflow step prepares the current page. download/save_page/open_tab/switch_tab are disabled."
                + " External tools allowed in this step: " + json.dumps(step.get("allow_tools", []))
                + ". Skill discovery/read tools remain available. Stay within these origins: " + json.dumps(self.allowed))
        result = await run_agent(task, config, self.runtime, controller=self.controller, completion_check=verify, action_guard=guard)
        self.state["page_url"] = self.controller.page.url
        compact = {key: result.get(key) for key in ("status", "answer", "log_file", "step", "verification", "completion_basis")}
        if result["status"] != "completed":
            # Retain running marker for a possibly side-effecting partial step.
            self.state["step_results"][step["id"]]["last_attempt"] = compact
            raise WorkflowStop(result["status"], "Agent step did not satisfy the host completion checks")
        return compact

    async def execute(self):
        config = deepcopy(self.config)
        config.setdefault("browser", {})["start_url"] = "about:blank"
        try:
            self.controller = await BrowserController.launch(config)
            # Reject off-origin browser requests at the navigation boundary. This is
            # collection scoping, not an OS/network sandbox for subresources/tools.
            async def route_navigation(route):
                if route.request.is_navigation_request() and route.request.frame.page == self.controller.page:
                    try:
                        scoped_url(route.request.url, self.allowed)
                    except ValueError:
                        await route.abort()
                        return
                await route.continue_()
            await self.controller.context.route("**/*", route_navigation)
            if self.state.get("page_url") and self.state["step_index"] < len(self.spec["steps"]):
                current = self.spec["steps"][self.state["step_index"]]
                if current["type"] != "crawl" or current["id"] not in self.state["cursors"]:
                    await self.visit(self.state["page_url"])
            while self.state["step_index"] < len(self.spec["steps"]):
                self.boundary()
                step = self.spec["steps"][self.state["step_index"]]
                self.event("step_start", step_id=step["id"], type=step["type"])
                if step["type"] == "navigate":
                    url = await self.visit(step["url"], step.get("wait_for"))
                    result = {"status": "completed", "url": url}
                elif step["type"] == "agent":
                    result = await self.agent_step(step)
                else:
                    result = await self.crawl(step)
                self.state["step_results"][step["id"]] = result
                self.state["step_index"] += 1
                self.state["page_url"] = self.controller.page.url
                self.store.save(self.state)
                self.event("step_completed", step_id=step["id"], result=result)
            count = self.store.count(self.state["run_id"])
            minimum = self.spec.get("min_records", 1 if any(s["type"] == "crawl" for s in self.spec["steps"]) else 0)
            verified = count >= minimum
            self.state["verification"] = {"ok": verified, "record_count": count, "min_records": minimum,
                                          "all_steps_completed": True, "required_fields_checked": True}
            self.state.update(status="completed" if verified else "incomplete", completion_basis="host_verified" if verified else "unverified")
            if not verified:
                self.state["error"] = "Minimum record count not reached"
        finally:
            try:
                if self.runtime:
                    await self.runtime.__aexit__(None, None, None)
            finally:
                if self.controller:
                    try:
                        await self.controller.context.unroute("**/*", route_navigation)
                    finally:
                        await self.controller.close()


async def run_workflow(name: str, config: dict, parameters: dict | None = None, *, resume_run_id: str | None = None,
                       page_budget: int | None = None, retry_uncertain_step: bool = False) -> dict:
    config = deepcopy(config)
    settings = config.get("workflows", {})
    spec = WorkflowCatalog(settings.get("paths", [])).get(name)
    if page_budget is not None and (isinstance(page_budget, bool) or not isinstance(page_budget, int) or page_budget < 1):
        raise ValueError("page_budget must be a positive integer")
    timeout = float(config.get("harness", {}).get("run_timeout_seconds", 600))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("run_timeout_seconds must be positive and finite")
    store = WorkflowStore(settings.get("state_dir", "workflow-runs"))
    state = None
    acquired = False
    try:
        if resume_run_id:
            state = store.get(resume_run_id)
            if state["workflow"] != name or state["spec_hash"] != digest(spec):
                raise ValueError("Workflow definition changed; resume requires the exact original version/content")
            if parameters is not None and render(spec, parameters)[1] != state["parameters"]:
                raise ValueError("Cannot change parameters while resuming a run")
            if state["status"] == "completed":
                return store.export(state)
        else:
            concrete, values = render(spec, parameters)
            state = store.create(spec, concrete, values)
        store.acquire(state["run_id"], max(60, timeout + 60))
        acquired = True
        # Read again after acquiring: another worker might have finished meanwhile.
        state = store.get(state["run_id"])
        if state["status"] == "completed":
            return store.export(state)
        state.update(status="running", attempt=state["attempt"] + 1, error=None, completion_basis="unverified")
        store.save(state)
        execution = WorkflowExecution(config, store, state, page_budget, retry_uncertain_step)
        execution.event("attempt_start")
        operation = asyncio.create_task(execution.execute())
        try:
            done, _ = await asyncio.wait({operation}, timeout=timeout)
            if not done:
                state.update(status="timeout", error="Workflow deadline reached; resume from the last page checkpoint")
                operation.cancel()
            try:
                await operation
            except asyncio.CancelledError:
                if state["status"] != "timeout":
                    raise
        except WorkflowStop as exc:
            state.update(status=exc.status, error=str(exc))
        except asyncio.CancelledError:
            state.update(status="cancelled", error="Caller cancelled the workflow")
            operation.cancel()
            try:
                await operation
            except asyncio.CancelledError:
                pass
            store.save(state)
            store.export(state)
            execution.event("attempt_end", status=state["status"])
            raise
        except Exception as exc:
            state.update(status="failed", error=str(exc)[:2000])
        store.save(state)
        execution.event("attempt_end", status=state["status"], error=state.get("error"))
        return store.export(state)
    finally:
        if acquired:
            store.release(state["run_id"])
        store.close()
