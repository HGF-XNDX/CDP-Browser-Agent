"""Opt-in live web + port-30000 acceptance. Does not run in CI or pytest."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import zipfile

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.web.tools import WebTools


async def evaluate(tag):
    output = ROOT / "logs/live-web-tools" / tag
    output.mkdir(parents=True, exist_ok=False)
    source = sorted((ROOT / "cdp_browser_agent").rglob("*.py"))
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in source + [Path(__file__)]:
            archive.write(path, path.relative_to(ROOT))
    config = browser_agent_default_config()
    config["model"].update(baseUrl="http://127.0.0.1:30000/v1", maxTokens=1536, maxRetries=0, apiTimeout=90)
    config["browser"].update(headless=True, focus_page=False, downloads_path=str(output / "downloads"))
    config["agent"].update(max_steps=8, log_dir=str(output / "events"))
    config["harness"]["run_timeout_seconds"] = 180
    config["web"].update(artifact_dir=str(output / "web"), allowed_private_hosts=["127.0.0.1"])
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        models = (await client.get(config["model"]["baseUrl"] + "/models")).json()
    config["model"]["model"] = models["data"][0]["id"]
    (output / "metadata.json").write_text(json.dumps({"models": models, "endpoint": config["model"]["baseUrl"],
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source}}, indent=2), encoding="utf-8")
    checks, observations = {}, {}
    web = WebTools(config["web"])
    started = time.monotonic()
    search = await web.search("Python official documentation", max_results=5)
    observations["search"] = {**search, "elapsed_seconds": round(time.monotonic() - started, 3)}
    checks["live_free_search"] = search["ok"] and any("python.org" in r["url"] for r in search["results"])
    print(json.dumps({"phase": "search", "ok": checks["live_free_search"], "route": search.get("network_route")}), flush=True)
    started = time.monotonic()
    fetch = await web.fetch("https://ipc.court.gov.cn/zh-cn/news/view-6071.html")
    observations["fetch"] = {**fetch, "elapsed_seconds": round(time.monotonic() - started, 3)}
    checks["live_source_fetch"] = fetch["ok"] and "（2022）最高法知民终2527号" in fetch["text"]
    print(json.dumps({"phase": "fetch", "ok": checks["live_source_fetch"], "route": fetch.get("network_route")}), flush=True)

    real_send, model_calls = httpx.AsyncClient.send, []
    async def traced_send(client, request, **kwargs):
        response = await real_send(client, request, **kwargs)
        if str(request.url).startswith(config["model"]["baseUrl"]) and request.method == "POST":
            await response.aread()
            wire = {"request": json.loads(request.content), "response": response.json()}
            model_calls.append(wire)
            with (output / "model-wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(wire, ensure_ascii=False) + "\n")
        return response
    httpx.AsyncClient.send = traced_send
    try:
        fast = await run_browser_agent("先搜索 Python 官方文档入口，再读取搜索结果中一个 python.org 官方网页，报告页面标题、它提供什么信息和实际来源链接。只做信息读取，无需操作表单。", config)
        observations["fast_agent"] = fast
        tools = [h["action"].get("name") for h in fast["history"] if h["action"]["action"] == "tool"]
        checks["real_model_search_and_fetch"] = fast["status"] == "completed" and "web_search" in tools and "web_fetch" in tools
        checks["no_browser_for_fast_task"] = not fast["browser_started"]
        checks["fetched_sources_in_final_state"] = any(s["kind"] == "web_fetch" for s in fast["sources"])
        print(json.dumps({"phase": "fast_agent", "status": fast["status"], "tools": tools, "browser_started": fast["browser_started"]}), flush=True)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                body = b'<html><title>Dynamic fixture</title><body><script>document.body.innerHTML="<h1>Inventory report</h1><p>Verified current inventory: SKU A17 has 42 units. This content is rendered by JavaScript.</p>"</script></body></html>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=site.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{site.server_port}/"
        try:
            # Local fixture bypasses proxy explicitly; public checks above use auto.
            config["web"]["fetch"] = {"proxy": ""}
            fallback = await run_browser_agent(f"读取 {url} 的库存报告，给出 SKU A17 的库存数量和来源。优先快速读取；如果静态页面没有正文，请切换浏览器读取。", config)
            observations["fallback_agent"] = fallback
            actions = [h["action"] for h in fallback["history"]]
            checks["real_model_browser_fallback"] = fallback["status"] == "completed" and fallback["browser_started"] and "42" in fallback["answer"]
            checks["fetch_precedes_browser"] = bool(actions) and actions[0].get("name") == "web_fetch" and any(a["action"] in {"navigate", "observe_browser"} for a in actions[1:])
            print(json.dumps({"phase": "fallback_agent", "status": fallback["status"], "browser_started": fallback["browser_started"]}), flush=True)
        finally:
            site.shutdown()
            site.server_close()
            worker.join(timeout=2)
    finally:
        httpx.AsyncClient.send = real_send
    config["web"].pop("fetch", None)
    config_file = output / "config.json"
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    before = len(model_calls)
    with (output / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
        async with Client(stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "cdp_browser_agent.mcp_server", "--config", str(config_file)], cwd=str(ROOT)), errlog=stderr), read_timeout_seconds=45) as client:
            mcp = await client.call_tool("web_fetch", {"url": "https://example.com"})
    observations["stdio_mcp_fetch"] = mcp.structured_content
    checks["real_stdio_mcp_fetch"] = not mcp.is_error and mcp.structured_content["ok"] and mcp.structured_content["title"] == "Example Domain"
    receipt = {"all_passed": all(checks.values()), "checks": checks, "direct_model_calls": len(model_calls),
               "observations": observations, "model": config["model"]["model"]}
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": checks, "receipt": str(output / "receipt.json")}, ensure_ascii=True), flush=True)
    if not all(checks.values()):
        raise RuntimeError("Live acceptance has failures; inspect preserved receipt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    asyncio.run(evaluate(parser.parse_args().tag))
