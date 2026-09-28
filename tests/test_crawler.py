import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.controller import BrowserController
from cdp_browser_agent.crawler.engine import Crawler
from cdp_browser_agent.crawler.store import CrawlStore, CrawlBusy
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.workflows.runner import run_workflow
from examples.crawling.demo_site import demo_server


@pytest.fixture
def site(tmp_path):
    config = browser_agent_default_config()
    config["web"].update(proxy="", allowed_private_hosts=["127.0.0.1"], artifact_dir=str(tmp_path / "web"))
    config["crawler"].update(state_dir=str(tmp_path / "crawls"), request_delay_seconds=0)
    config["agent"]["log_dir"] = str(tmp_path / "logs")
    config["processing"]["artifact_dir"] = str(tmp_path / "processed")
    with demo_server() as (server, url):
        yield config, server, url


def listing(url, **overrides):
    return {"seed_urls": [url + "/list?page=1"], "max_pages": 5, "max_depth": 0,
            "next_selector": "a.next", "item_selector": "article.item",
            "fields": {"id": {"attribute": "data-id"}, "title": {"selector": "h2"}},
            "key_fields": ["id"], **overrides}


def mapping(config, tmp_path):
    profile = {"name": "titles", "mode": "mapping", "instructions": "Keep original titles",
        "field_map": {"title": "title"}, "output_schema": {"type": "object", "properties": {"title": {"type": "string"}},
            "required": ["title"], "additionalProperties": False}, "formats": ["json", "csv", "markdown"]}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    config["processing"]["paths"] = [str(path)]


async def test_listing_checkpoint_resume_and_full_evidence(site):
    config, server, url = site
    service = Crawler(config, parent_id="a"*32)
    first = await service.run(listing(url), page_budget=1)
    assert first["status"] == "paused" and first["record_count"] == 2
    old_files = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in first["artifact_paths"]}
    second = await Crawler(config, parent_id="a"*32).run(crawl_id=first["crawl_id"])
    assert second["ok"] and second["record_count"] == 3 and second["duplicate_records"] == 1, second
    assert [p for p, _ in server.requests].count("/list?page=1") == 1
    assert second["model_calls"] == 0 and not second["browser_started"]
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in old_files.items())
    records = service.records(first["crawl_id"])
    assert [r["data"]["title"] for r in records] == ["Alpha", "Beta", "Gamma"]
    assert all(hashlib.sha256(Path(r["artifact_paths"][1]).read_bytes()).hexdigest() == r["response_sha256"] for r in records)
    assert service.read(first["crawl_id"], limit=80)["next_offset"] == 80
    with pytest.raises(ValueError, match="parent"):
        Crawler(config, parent_id="other").records(first["crawl_id"])
    with pytest.raises(ValueError, match="exactly one"):
        await service.run(listing(url), crawl_id=first["crawl_id"])


async def test_scope_depth_dedup_and_limits(site):
    config, server, url = site
    service = Crawler(config)
    result = await service.run({"seed_urls": [url+"/index"], "max_depth": 1, "exclude_patterns": ["*/excluded"], "record_patterns": ["*/detail/*"]})
    assert result["ok"] and result["record_count"] == 2 and result["pages_finished"] == 3
    assert [p for p, _ in server.requests].count("/detail/a") == 1
    assert "/excluded" not in [p for p, _ in server.requests]
    partial = await service.run(listing(url, max_pages=1))
    assert partial["status"] == "incomplete" and partial["pending_pages"] == 1 and not partial["scope_completed"]
    with pytest.raises(ValueError, match="incomplete"):
        service.records(partial["crawl_id"])
    bounded = await service.run(listing(url, max_records=1))
    assert bounded["status"] == "incomplete" and bounded["record_count"] == 1


async def test_robots_redirect_and_dynamic_fallback(site):
    config, server, url = site
    service = Crawler(config)
    blocked = await service.run({"seed_urls": [url+"/redirect"], "max_depth": 0})
    assert blocked["status"] == "incomplete" and not blocked["needs_browser"]
    assert not any(p.startswith("/blocked") for p, _ in server.requests)
    server.redirect = "https://example.org/outside"
    outside = await service.run({"seed_urls": [url+"/redirect"], "max_depth": 0})
    assert outside["failed_pages"] == 1 and not outside["needs_browser"]
    dynamic = await service.run({"seed_urls": [url+"/dynamic", url+"/login"], "max_depth": 0})
    assert dynamic["needs_browser"] and len(dynamic["browser_fallback"]) == 2 and not dynamic["browser_started"]


async def test_retry_after_persists_across_resume(site):
    config, server, url = site
    config["crawler"]["max_retries"] = 1
    service = Crawler(config)
    first = await service.run({"seed_urls": [url+"/rate"], "max_depth": 0})
    assert first["status"] == "paused" and first["reason"] == "retry_backoff"
    again = await service.run(crawl_id=first["crawl_id"])
    assert again["request_attempts"] == 1
    await asyncio.sleep(max(0, first["next_retry_at"]-time.time())+.03)
    last = await Crawler(config).run(crawl_id=first["crawl_id"])
    assert last["status"] == "incomplete" and not last["needs_browser"] and last["request_attempts"] == 2
    times = [t for p, t in server.requests if p == "/rate"]
    assert times[1]-times[0] >= 1


async def test_required_fields_key_conflict_and_integrity(site):
    config, _, url = site
    service = Crawler(config)
    missing = await service.run(listing(url, fields={"id": {"selector": ".missing"}}, key_fields=["id"]))
    assert missing["failed_pages"] == 1 and missing["record_count"] == 0
    conflict = await service.run(listing(url, seed_urls=[url+"/conflict"]))
    assert conflict["failed_pages"] == 1 and conflict["record_count"] == 0
    good = await service.run(listing(url))
    root = Path(config["crawler"]["state_dir"])
    dataset = root / good["crawl_id"] / "artifacts" / (good["dataset"]["artifact_id"]+".json")
    dataset.write_text("[]")
    with pytest.raises(ValueError, match="hash mismatch"):
        service.read(good["crawl_id"])
    with CrawlStore(root) as store:
        state = store.get(good["crawl_id"])
        record = store.artifacts(good["crawl_id"])._path(state["records"][0]["artifact_id"])
        record.write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        service.records(good["crawl_id"])


async def test_pause_inflight_recovery_and_lease(site):
    config, server, url = site
    service = Crawler(config)
    identity = service.create({"seed_urls": [url+"/slow"], "max_depth": 0})["crawl_id"]
    operation = asyncio.create_task(service.run(crawl_id=identity))
    for _ in range(100):
        if any(p == "/slow" for p, _ in server.requests):
            break
        await asyncio.sleep(.02)
    busy = await Crawler(config).run(crawl_id=identity)
    assert busy["status"] == "busy"
    service.pause(identity)
    stopped = await operation
    assert stopped["status"] == "paused" and stopped["reason"] == "pause_requested"
    resumed = await Crawler(config).run(crawl_id=identity)
    assert resumed["ok"] and resumed["record_count"] == 1
    with CrawlStore(service.root) as store:
        created = store.create({"seed_urls": [url], "max_depth": 0})
        state, _ = store.acquire(created["crawl_id"])
        state["queue"][0]["status"] = "fetching"
        store.save(state)
        store.db.execute("UPDATE crawls SET lease=0 WHERE id=?", (state["crawl_id"],))
        store.db.commit()
        with CrawlStore(service.root) as recovered:
            current, acquired = recovered.acquire(state["crawl_id"])
            assert acquired and current["queue"][0]["status"] == "pending"
            with pytest.raises(CrawlBusy):
                store.save(state)


async def test_runtime_handoff_full_dataset_parent_binding_and_restart(site, tmp_path):
    config, _, url = site
    mapping(config, tmp_path)
    runtime = ExtensionRuntime(config)
    runtime.task_state = {"run_id": "a"*32}
    result = await runtime.registry._tools["web_crawl"].handler(spec=listing(url, seed_urls=[url+"/many"]))
    assert result["record_count"] == 25
    resumed = ExtensionRuntime(config)
    resumed.task_state = {"run_id": "a"*32, "sources": []}
    assert resumed.context()["crawls"][0]["crawl_id"] == result["crawl_id"]
    processed = await resumed.registry._tools["delegate_processing"].handler(profile="titles", crawl_id=result["crawl_id"])
    assert processed["ok"] and processed["validated_count"] == 25
    assert processed["output_preview"]["truncated"] and processed["output_preview"]["total_records"] == 25
    assert 0 < len(processed["output_preview"]["records"]) <= 8
    assert len(json.loads(Path(processed["records_path"]).read_text(encoding="utf-8"))) == 25


async def test_workflow_http_crawl_resume_process_no_browser(site, tmp_path, monkeypatch):
    config, _, url = site
    mapping(config, tmp_path)
    browser = AsyncMock(side_effect=AssertionError("HTTP workflow must not launch a browser"))
    monkeypatch.setattr(BrowserController, "launch", browser)
    workflow = {"schema_version": 1, "name": "collect", "version": "1", "description": "Static pipeline", "allowed_origins": [url], "min_records": 3,
        "steps": [{"id": "collect", "type": "http_crawl", "spec": listing(url)}, {"id": "format", "type": "process", "input_step": "collect", "profile": "titles"}]}
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(workflow), encoding="utf-8")
    config["workflows"] = {"paths": [str(path)], "state_dir": str(tmp_path / "workflows")}
    first = await run_workflow("collect", config, page_budget=1)
    assert first["status"] == "paused", first
    done = await run_workflow("collect", config, resume_run_id=first["run_id"])
    assert done["status"] == "completed" and done["record_count"] == 3, done
    browser.assert_not_awaited()
    workflow["allowed_origins"] = ["https://example.org"]
    path.write_text(json.dumps(workflow), encoding="utf-8")
    rejected = await run_workflow("collect", config)
    assert rejected["status"] == "failed" and "origins" in rejected["error"]


async def test_real_stdio_mcp_crawler_to_worker(site, tmp_path):
    config, _, url = site
    mapping(config, tmp_path)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    async with Client(StdioServerParameters(command=sys.executable, args=["-m", "cdp_browser_agent.mcp_server", "--config", str(path)])) as client:
        first = await client.call_tool("web_crawl", {"spec": listing(url)})
        assert not first.is_error, first
        identity = first.structured_content["crawl_id"]
        status = await client.call_tool("web_crawl_status", {"crawl_id": identity})
        assert status.structured_content["record_count"] == 3
        result = await client.call_tool("browser_worker_start", {"profile": "titles", "crawl_id": identity})
        assert not result.is_error and result.structured_content["validated_count"] == 3, result


async def test_robots_delay_survives_deadline_without_phantom_reservations(site, monkeypatch):
    from types import SimpleNamespace
    from cdp_browser_agent.crawler import store as storage

    config, server, url = site
    # Give the real local HTTP connection time to start on Windows. Freeze only
    # the rate-limit clock so the deadline always interrupts a robots delay,
    # regardless of how long HTTP client initialization takes.
    clock = [time.time()]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    config["crawler"]["run_timeout_seconds"] = 3
    server.robots += "Crawl-delay: 0.4\n"
    service = Crawler(config)
    first = await service.run({"seed_urls": [url+"/detail/a"], "max_depth": 0})
    assert first["status"] == "paused" and first["reason"] == "deadline"
    assert [p for p, _ in server.requests] == ["/robots.txt"]
    clock[0] += .4
    done = await service.run(crawl_id=first["crawl_id"])
    assert done["ok"], done
    assert [p for p, _ in server.requests] == ["/robots.txt", "/detail/a"]
    with CrawlStore(service.root) as store:
        reserved = store.db.execute("SELECT last_request FROM origins").fetchone()[0]
    assert reserved == clock[0]


async def test_caller_cancellation_is_checkpointed(site):
    config, _, url = site
    service = Crawler(config)
    identity = service.create({"seed_urls": [url+"/slow"], "max_depth": 0})["crawl_id"]
    task = asyncio.create_task(service.run(crawl_id=identity))
    await asyncio.sleep(.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = service.status(identity)
    assert status["status"] == "paused" and not status["active"] and status["pending_pages"] == 1
    assert (await service.run(crawl_id=identity))["ok"]


async def test_operator_limits_disable_and_private_address_boundaries(site):
    config, server, url = site
    previous = Crawler(config).create(listing(url))["crawl_id"]
    config["crawler"]["max_pages"] = 1
    service = Crawler(config)
    with pytest.raises(ValueError, match="operator limit"):
        await service.run(crawl_id=previous)
    with pytest.raises(ValueError, match="operator limit"):
        service.create(listing(url))
    assert (await service.run({"seed_urls": [url+"/detail/a"], "max_depth": 0}))["ok"]
    config["web"]["allowed_private_hosts"] = []
    private = await Crawler(config).run({"seed_urls": [url+"/private"], "max_depth": 0})
    assert not private["needs_browser"] and not private["ok"]
    assert "/private" not in [p for p, _ in server.requests]
    config["web"]["enabled"] = False
    with pytest.raises(ValueError, match="disabled"):
        Crawler(config).create({"seed_urls": [url]})


def test_shared_throttle_and_html_base_resolution(tmp_path, monkeypatch):
    from cdp_browser_agent.crawler import store as storage
    from cdp_browser_agent.crawler.spec import parse_page, prepare
    clock = [100.0]
    monkeypatch.setattr(storage.time, "time", lambda: clock[0])
    with CrawlStore(tmp_path) as one, CrawlStore(tmp_path) as two:
        assert one.reserve_request("site", .5) == 0
        assert two.reserve_request("site", 2) == 2
        assert two.reserve_request("site", 2) == 2  # Waiting never advances the slot.
        clock[0] = 102
        assert two.reserve_request("site", 2) == 0
        one.cooldown("site", 10)
        assert two.reserve_request("site", 0) == 10
    spec = prepare({"seed_urls": ["https://example.com/list"], "fields": {"url": {"selector": "a", "attribute": "href", "url": True}}}, {"max_pages": 200, "max_records": 5000})
    rows, links = parse_page(b'<base href="/documents/"><a href="one">One</a>', "text/html", "https://example.com/list", "One", "", spec, 0)
    assert rows[0]["url"] == links[0]["url"] == "https://example.com/documents/one"


async def test_html_inspection_paging_integrity_and_root_field_semantics(site):
    from cdp_browser_agent.web.tools import WebTools
    config, server, url = site
    web = WebTools(config["web"], artifact_root=Path(config["web"]["artifact_dir"]))
    first = await web.fetch(url+"/many", max_chars=500, content_format="html")
    assert first["ok"] and first["content_format"] == "html" and 'data-id="0"' in first["text"]
    offset, chunks = first["next_offset"], [first["text"]]
    while offset is not None:
        chunk = await web.fetch(url+"/many", offset=offset, max_chars=500, content_format="html")
        assert chunk["cache_hit"] and chunk["content_format"] == "html"
        chunks.append(chunk["text"])
        offset = chunk["next_offset"]
    assert ''.join(chunks) == Path(first["artifact_paths"][1]).read_bytes().decode()
    assert [p for p, _ in server.requests].count("/many") == 1
    assert '<article' not in Path(first["artifact_paths"][0]).read_text(encoding="utf-8")
    Path(first["artifact_paths"][1]).write_bytes(b"changed")
    changed = await web.fetch(url+"/many", offset=1, content_format="html")
    assert not changed["ok"] and changed["status"] == "evidence_changed"
    done = await Crawler(config).run(listing(url, fields={"id": {"selector": ":scope", "attribute": "data-id"}, "title": {"selector": "h2"}}))
    assert done["ok"] and done["record_count"] == 3
    failed = await Crawler(config).run(listing(url, fields={"id": {"selector": "[data-id]", "attribute": "data-id"}}))
    assert "omit selector" in failed["failures"][0]["reason"]
