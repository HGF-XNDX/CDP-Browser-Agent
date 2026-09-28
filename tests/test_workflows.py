import asyncio
import hashlib
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from cdp_browser_agent.browser import agent
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.mcp_server import create_mcp_server
from cdp_browser_agent.workflows.runner import run_workflow, WorkflowExecution
from cdp_browser_agent.workflows.spec import render, scoped_url, validate_spec
from cdp_browser_agent.workflows.store import WorkflowStore, WorkflowBusy
from examples.workflows.demo_site import demo_server


DEMO = Path(__file__).parents[1] / "examples/workflows/demo-catalog.json"


@pytest.fixture
def setup_workflow(tmp_path, monkeypatch):
    with demo_server() as (server, url):
        spec = json.loads(DEMO.read_text(encoding="utf-8"))
        spec["steps"] = [spec["steps"][-1]]
        spec["steps"][0]["start_url"] = "${base_url}/catalog?page=1"
        spec["request_delay_ms"] = 0
        path = tmp_path / "workflow.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        config = browser_agent_default_config()
        config["browser"].update(headless=True, focus_page=False, downloads_path=str(tmp_path / "downloads"))
        config["workflows"] = {"paths": [str(path)], "state_dir": str(tmp_path / "runs")}
        config["agent"]["log_dir"] = str(tmp_path / "logs")
        config["harness"]["run_timeout_seconds"] = 30
        planner = AsyncMock(side_effect=AssertionError("A deterministic crawl must not call the model"))
        monkeypatch.setattr(agent, "plan_next_action", planner)
        yield spec, path, config, server, url, planner


def read_records(result):
    return [json.loads(line) for line in (Path(result["output_dir"]) / "records.jsonl").read_text(encoding="utf-8").splitlines()]


async def test_pause_resume_and_incremental_changes(setup_workflow):
    spec, path, config, server, url, planner = setup_workflow
    first = await run_workflow("demo-catalog", config, {"base_url": url}, page_budget=1)
    assert first["status"] == "paused" and first["record_count"] == 2, first
    requests = server.requests["/catalog?page=1"]
    resumed = await run_workflow("demo-catalog", config, resume_run_id=first["run_id"])
    assert resumed["run_id"] == first["run_id"] and resumed["attempt"] == 2
    assert resumed["status"] == "completed" and resumed["record_count"] == 3, resumed
    assert resumed["completion_basis"] == "host_verified"
    assert server.requests["/catalog?page=1"] == requests
    records = read_records(resumed)
    for record in records:
        for key in ("page_sha256", "detail_sha256"):
            snapshot = Path(resumed["output_dir"]) / "snapshots" / (record[key] + ".html")
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == record[key]
    assert {r["data"]["sku"] for r in records} == {"A17", "B28", "C39"}
    assert all("Verified details" in r["data"]["description"] and r["source_url"].startswith(url + "/product/") for r in records)
    assert resumed["changes"] == {"new": 3, "changed": 0, "unchanged": 0}
    old_export = (Path(resumed["output_dir"]) / "records.jsonl").read_bytes()
    server.products["B28"] = ("Desk lamp Pro", "169")
    latest = await run_workflow("demo-catalog", config, {"base_url": url})
    assert latest["baseline_run_id"] == resumed["run_id"]
    assert latest["changes"] == {"new": 0, "changed": 1, "unchanged": 2}
    assert (Path(resumed["output_dir"]) / "records.jsonl").read_bytes() == old_export
    planner.assert_not_awaited()


async def test_page_failure_resumes_without_losing_committed_rows(setup_workflow):
    _, _, config, server, url, _ = setup_workflow
    server.fail_paths.add("/catalog?page=2")
    failed = await run_workflow("demo-catalog", config, {"base_url": url})
    assert failed["status"] == "failed" and failed["record_count"] == 2, failed
    requests = server.requests["/catalog?page=1"]
    server.fail_paths.clear()
    recovered = await run_workflow("demo-catalog", config, resume_run_id=failed["run_id"])
    assert recovered["status"] == "completed" and recovered["record_count"] == 3
    assert server.requests["/catalog?page=1"] == requests


async def test_detail_failure_preserves_previous_detail_checkpoint(setup_workflow):
    _, _, config, server, url, _ = setup_workflow
    server.fail_paths.add("/product/B28")
    first = await run_workflow("demo-catalog", config, {"base_url": url})
    assert first["status"] == "failed"
    requests = server.requests["/product/A17"]
    assert requests == 1
    server.fail_paths.clear()
    resumed = await run_workflow("demo-catalog", config, resume_run_id=first["run_id"])
    assert resumed["status"] == "completed" and resumed["record_count"] == 3
    assert server.requests["/product/A17"] == requests


async def test_missing_fields_and_unknown_empty_page_fail(setup_workflow):
    spec, path, config, _, url, _ = setup_workflow
    spec["steps"][0]["fields"]["title"]["selector"] = ".missing-title"
    path.write_text(json.dumps(spec), encoding="utf-8")
    result = await run_workflow("demo-catalog", config, {"base_url": url})
    assert result["status"] == "failed" and result["record_count"] == 0
    assert "Required field missing" in result["error"]
    spec["steps"][0]["start_url"] = "${base_url}/empty"
    spec["steps"][0].pop("wait_for", None)
    path.write_text(json.dumps(spec), encoding="utf-8")
    unknown = await run_workflow("demo-catalog", config, {"base_url": url})
    assert unknown["status"] == "failed" and "No records" in unknown["error"]
    spec["steps"][0]["empty_selector"] = ".empty"
    spec["min_records"] = 0
    path.write_text(json.dumps(spec), encoding="utf-8")
    empty = await run_workflow("demo-catalog", config, {"base_url": url})
    assert empty["status"] == "completed" and empty["record_count"] == 0


async def test_limit_does_not_claim_full_collection_and_definition_is_frozen(setup_workflow):
    spec, path, config, _, url, _ = setup_workflow
    spec["steps"][0]["max_pages"] = 1
    path.write_text(json.dumps(spec), encoding="utf-8")
    limited = await run_workflow("demo-catalog", config, {"base_url": url})
    assert limited["status"] == "incomplete" and limited["record_count"] == 2
    spec["steps"][0]["max_pages"] = 2
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="definition changed"):
        await run_workflow("demo-catalog", config, resume_run_id=limited["run_id"])


async def test_mcp_workflow_catalog_run_status_and_pause(setup_workflow):
    _, _, config, _, url, _ = setup_workflow
    async with Client(create_mcp_server(config)) as client:
        catalog = await client.call_tool("browser_workflows", {})
        assert catalog.structured_content["workflows"][0]["name"] == "demo-catalog"
        result = await client.call_tool("browser_workflow_run", {"name": "demo-catalog", "parameters": {"base_url": url}, "page_budget": 1})
        assert not result.is_error, result
        run = result.structured_content
        assert run["status"] == "paused" and run["record_count"] == 2
        status = await client.call_tool("browser_workflow_status", {"run_id": run["run_id"]})
        assert status.structured_content["status"] == "paused"
        pause = await client.call_tool("browser_workflow_pause", {"run_id": run["run_id"]})
        assert pause.structured_content["pause_requested"]
        invalid = await client.call_tool("browser_workflow_run", {"name": "unconfigured", "parameters": {}})
        assert invalid.is_error
        completed = await client.call_tool("browser_workflow_run", {"name": "demo-catalog", "resume_run_id": run["run_id"]})
        assert completed.structured_content["status"] == "completed"


async def test_cancellation_persists_checkpoint(setup_workflow, monkeypatch):
    _, _, config, _, url, _ = setup_workflow
    original = WorkflowExecution.visit
    entered = asyncio.Event()
    async def wait_at_page_two(self, target, *args):
        if target.endswith("page=2"):
            entered.set()
            await asyncio.sleep(100)
        return await original(self, target, *args)
    monkeypatch.setattr(WorkflowExecution, "visit", wait_at_page_two)
    operation = asyncio.create_task(run_workflow("demo-catalog", config, {"base_url": url}))
    await asyncio.wait_for(entered.wait(), 20)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    store = WorkflowStore(config["workflows"]["state_dir"])
    try:
        run_id = store.db.execute("SELECT run_id FROM runs").fetchone()[0]
        state = store.get(run_id)
        assert state["status"] == "cancelled" and store.count(run_id) == 2
        assert state["cursors"]["products"]["next_url"].endswith("page=2")
    finally:
        store.close()


async def test_live_cooperative_pause_at_page_boundary(setup_workflow, monkeypatch):
    _, _, config, server, url, _ = setup_workflow
    original = WorkflowExecution.event
    def pause_after_page(self, event, **data):
        original(self, event, **data)
        if event == "page_committed":
            self.store.request_pause(self.state["run_id"])
    monkeypatch.setattr(WorkflowExecution, "event", pause_after_page)
    result = await run_workflow("demo-catalog", config, {"base_url": url})
    assert result["status"] == "paused" and result["record_count"] == 2
    assert server.requests["/catalog?page=2"] == 0


async def test_uncertain_agent_step_is_not_automatically_replayed(setup_workflow):
    spec, path, config, _, url, planner = setup_workflow
    config["intervention"]["mode"] = "return"
    spec["steps"] = [{"id": "submit", "type": "agent", "instructions": "Submit a form",
                      "checks": [{"kind": "visible", "selector": "#receipt"}]}]
    path.write_text(json.dumps(spec), encoding="utf-8")
    concrete, values = render(spec, {"base_url": url})
    store = WorkflowStore(config["workflows"]["state_dir"])
    try:
        state = store.create(spec, concrete, values)
        store.acquire(state["run_id"], 60)
        state["step_results"]["submit"] = {"status": "running"}
        state["status"] = "cancelled"
        store.save(state)
        store.release(state["run_id"])
    finally:
        store.close()
    result = await run_workflow("demo-catalog", config, resume_run_id=state["run_id"])
    assert result["status"] == "needs_input" and "interrupted" in result["error"]
    planner.assert_not_awaited()


def test_scope_parameters_and_lease(tmp_path):
    spec = json.loads(DEMO.read_text(encoding="utf-8"))
    validate_spec(spec)
    concrete, params = render(spec, {"base_url": "http://127.0.0.1:12345"})
    with pytest.raises(ValueError, match="outside"):
        scoped_url("https://other.example/private", concrete["allowed_origins"])
    with pytest.raises(Exception):
        render(spec, {"base_url": "https://other.example", "unknown": "x"})
    one, two = WorkflowStore(tmp_path), WorkflowStore(tmp_path)
    try:
        state = one.create(spec, concrete, params)
        one.acquire(state["run_id"], 60)
        with pytest.raises(WorkflowBusy):
            two.acquire(state["run_id"], 60)
        one.release(state["run_id"])
        two.acquire(state["run_id"], 60)
        two.release(state["run_id"])
    finally:
        one.close()
        two.close()


def test_url_parameters_and_site_templates():
    root = Path(__file__).parents[1]
    for path in (root / "examples/site-workflows").glob("*.json"):
        validate_spec(json.loads(path.read_text(encoding="utf-8")))
    spec = json.loads((root / "examples/workflows/example-domain.json").read_text(encoding="utf-8"))
    spec["parameters"] = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
    spec["steps"] = [{"id": "search", "type": "navigate", "url": "https://example.com/?q=${query:urlencode}"}]
    concrete, _ = render(spec, {"query": "A&B #1"})
    assert concrete["steps"][0]["url"] == "https://example.com/?q=A%26B%20%231"
