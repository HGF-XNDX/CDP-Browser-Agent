"""Explicit real-model website workflow acceptance; never run by pytest/CI."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.workflows.runner import run_workflow
from examples.workflows.demo_site import demo_server


def rows(result):
    return [json.loads(line) for line in (Path(result["output_dir"]) / "records.jsonl").read_text(encoding="utf-8").splitlines()]


async def evaluate(args):
    output = ROOT / "logs/live-workflows" / args.tag
    output.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(output / "source-snapshot.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        source = sorted((ROOT / "cdp_browser_agent").rglob("*.py"))
        for path in source + [Path(__file__), ROOT / "examples/workflows/demo_site.py", ROOT / "examples/workflows/demo-catalog.json"]:
            archive.write(path, path.relative_to(ROOT))
    async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
        model_discovery = (await http.get(args.base_url.rstrip("/") + "/models")).json()
    model = model_discovery["data"][0]["id"]
    (output / "metadata.json").write_text(json.dumps({"base_url": args.base_url, "model": model, "discovery": model_discovery,
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source}}, indent=2), encoding="utf-8")
    real_send = httpx.AsyncClient.send
    calls = []
    phase = ""
    async def traced_send(client, request, **kwargs):
        response = await real_send(client, request, **kwargs)
        if str(request.url).startswith(args.base_url) and request.method == "POST":
            await response.aread()
            data = response.json()
            record = {"phase": phase, "request": json.loads(request.content), "response": data}
            calls.append(record)
            with (output / "model-wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return response
    httpx.AsyncClient.send = traced_send
    checks = {}
    try:
        with demo_server() as (site, url):
            config = browser_agent_default_config()
            config["model"].update(baseUrl=args.base_url, model=model, maxTokens=1536, apiTimeout=90, maxRetries=0)
            config["browser"].update(headless=True, focus_page=False, downloads_path=str(output / "downloads"))
            config["agent"].update(max_steps=8, log_dir=str(output / "events"))
            config["harness"]["run_timeout_seconds"] = 180
            config["workflows"] = {"paths": [str(ROOT / "examples/workflows")], "state_dir": str(output / "runs")}
            phase = "initial_pause"
            initial = await run_workflow("demo-catalog", config, {"base_url": url}, page_budget=1)
            checks["paused_after_page_one"] = initial["status"] == "paused" and initial["record_count"] == 2
            print(json.dumps({"phase": phase, "result": initial}, ensure_ascii=True), flush=True)
            requests = site.requests["/catalog?page=1"]
            call_count = len(calls)
            phase = "resume"
            resumed = await run_workflow("demo-catalog", config, resume_run_id=initial["run_id"])
            collected = rows(resumed)
            checks["resumed_no_model_calls"] = len(calls) == call_count
            checks["did_not_refetch_committed_listing"] = site.requests["/catalog?page=1"] == requests
            checks["exact_records_and_details"] = ({r["data"]["sku"]: (r["data"]["price"], r["data"]["description"]) for r in collected}
                == {sku: (price, f"Verified details for {sku}.") for sku, (_, price) in site.products.items()})
            checks["host_verified_completion"] = resumed["status"] == "completed" and resumed["completion_basis"] == "host_verified"
            print(json.dumps({"phase": phase, "result": resumed}, ensure_ascii=True), flush=True)
            site.products["B28"] = ("Desk lamp Pro", "169")
            phase = "changed_price"
            latest = await run_workflow("demo-catalog", config, {"base_url": url})
            checks["exact_change_detection"] = latest["status"] == "completed" and latest["changes"] == {"new": 0, "changed": 1, "unchanged": 2}
            changed = [r for r in rows(latest) if r["change"] == "changed"]
            checks["changed_row_is_actual_price"] = len(changed) == 1 and changed[0]["data"]["sku"] == "B28" and changed[0]["data"]["price"] == "169"
            print(json.dumps({"phase": phase, "result": latest}, ensure_ascii=True), flush=True)
            phase = "stdio_mcp"
            config_path = output / "config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            with (output / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
                async with Client(stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "cdp_browser_agent.mcp_server", "--config", str(config_path)], cwd=str(ROOT)), errlog=stderr), read_timeout_seconds=200) as client:
                    result = await client.call_tool("browser_workflow_run", {"name": "demo-catalog", "parameters": {"base_url": url}})
            mcp = result.structured_content
            checks["real_stdio_workflow"] = not result.is_error and mcp["status"] == "completed" and mcp["record_count"] == 3 and mcp["changes"]["unchanged"] == 3
            phase = "public_fixed_site"
            public = await run_workflow("example-domain", config)
            checks["public_fixed_site"] = public["status"] == "completed" and rows(public)[0]["data"]["title"] == "Example Domain"
            receipt = {"model": model, "checks": checks, "all_passed": all(checks.values()), "direct_model_calls": len(calls),
                       "prompt_tokens": sum(c["response"].get("usage", {}).get("prompt_tokens", 0) for c in calls),
                       "initial": initial, "resumed": resumed, "changed": latest, "stdio_mcp": mcp, "public": public,
                       "model_call_note": "Direct calls captured in model-wire.jsonl; MCP subprocess calls are separate, see its agent events.",
                       "site_requests": dict(site.requests)}
            (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"checks": checks, "direct_model_calls": len(calls), "receipt": str(output / "receipt.json")}, ensure_ascii=True), flush=True)
            if not all(checks.values()):
                raise RuntimeError("Workflow acceptance checks failed; inspect the preserved receipt")
    finally:
        httpx.AsyncClient.send = real_send


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    asyncio.run(evaluate(parser.parse_args()))
