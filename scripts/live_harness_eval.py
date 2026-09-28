"""Opt-in 30000 model acceptance for collection, isolated processing and MCP delivery."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
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
from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.workflows.runner import run_workflow
from cdp_browser_agent.processing.engine import ProcessingEngine


async def evaluate(tag):
    output = ROOT / "logs/live-harness" / tag
    output.mkdir(parents=True, exist_ok=False)
    source = sorted((ROOT / "cdp_browser_agent").rglob("*.py"))
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in source + [Path(__file__)]:
            archive.write(path, path.relative_to(ROOT))
    config = load_config(str(ROOT / "examples/harness-30000.json"))
    config["agent"].update(max_steps=10, log_dir=str(output / "events"))
    config["harness"].update(run_timeout_seconds=240, tool_timeout_seconds=120)
    config["browser"]["downloads_path"] = str(output / "downloads")
    config["web"]["artifact_dir"] = str(output / "web")
    config["processing"]["artifact_dir"] = str(output / "processed")
    config["workflows"]["state_dir"] = str(output / "workflows")
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        models = (await client.get(config["model"]["baseUrl"] + "/models")).json()
    config["model"]["model"] = models["data"][0]["id"]
    config_file = output / "config.json"
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "metadata.json").write_text(json.dumps({"model": config["model"]["model"],
        "endpoint": config["model"]["baseUrl"], "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source}}, indent=2), encoding="utf-8")
    observations, checks, calls = {}, {}, []
    real_send = httpx.AsyncClient.send
    async def traced_send(client, request, **kwargs):
        response = await real_send(client, request, **kwargs)
        if str(request.url).startswith(config["model"]["baseUrl"]) and request.method == "POST":
            await response.aread()
            wire = {"request": json.loads(request.content), "response": response.json()}
            calls.append(wire)
            with (output / "model-wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(wire, ensure_ascii=False) + "\n")
        return response
    httpx.AsyncClient.send = traced_send
    try:
        workflow = await run_workflow("collect-and-process", config)
        observations["workflow"] = workflow
        checks["live_fetch_to_real_processing"] = workflow["status"] == "completed" and workflow["record_count"] == 1
        print(json.dumps({"phase": "workflow", "status": workflow["status"]}), flush=True)
        processed = next(iter(workflow.get("processing_results", {}).values()), {})
        if processed.get("records_path"):
            rows = json.loads(Path(processed["records_path"]).read_text(encoding="utf-8"))
            observations["processed_records"] = rows
            checks["schema_and_exact_quotes"] = len(rows) == 1 and rows[0]["validation_basis"] == "schema_and_source_quotes"
            checks["three_export_formats"] = {Path(p).suffix for p in processed["artifact_paths"]} >= {".json", ".csv", ".md"}
        agent = await run_browser_agent(
            "采集 https://example.com 的网页内容，并按已配置的 page-facts 方法交给独立处理子智能体，生成 JSON、CSV、Markdown 文件。"
            "先用 plan_update 建立简短计划，再 web_fetch，随后 delegate_processing，检查成功结果后报告文件路径和来源。不要重复调用已经成功的工具。", config)
        observations["agent"] = agent
        names = [h["action"].get("name") for h in agent["history"] if h["action"]["action"] == "tool"]
        checks["real_model_plans_collects_delegates"] = agent["status"] == "completed" and all(n in names for n in ("plan_update", "web_fetch", "delegate_processing"))
        checks["agent_has_validated_delivery"] = any(p["ok"] for p in agent.get("processing_results", []))
        checks["fast_task_no_browser"] = agent["browser_started"] is False
        checks["usage_reported"] = agent.get("metrics", {}).get("model_calls", 0) > 0
        print(json.dumps({"phase": "agent", "status": agent["status"], "tools": names}), flush=True)
        # A real model receives autonomous policy after asking an underspecified question.
        decision = await run_browser_agent(
            "请先用 ask_user 询问我网页摘要的语言偏好，之后按照系统人工介入策略继续。"
            "任务是使用 web_fetch 读取 https://example.com，依据正文写摘要并列出来源；没有语言回答时自行决定用中文。", config)
        observations["autonomous"] = decision
        checks["real_model_autonomous_continuation"] = decision["status"] == "completed" and any(d["mode"] == "auto" for d in decision.get("decisions", []))
        print(json.dumps({"phase": "autonomous", "status": decision["status"]}), flush=True)
    finally:
        httpx.AsyncClient.send = real_send
        receipt = {"all_passed": bool(checks) and all(checks.values()), "checks": checks,
                   "model": config["model"]["model"], "direct_model_calls": len(calls), "observations": observations}
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
        async with Client(stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "cdp_browser_agent.mcp_server", "--config", str(config_file)], cwd=str(ROOT)), errlog=stderr), read_timeout_seconds=150) as client:
            result = await client.call_tool("browser_process", {"profile": "page-facts", "records": [
                {"source_url": "https://example.com", "data": {"title": "Example Domain", "text": "This domain is for use in documentation examples without needing permission. Avoid use in operations."}}]})
            observations["stdio_mcp_processing"] = result.structured_content
            checks["real_stdio_processing"] = not result.is_error and result.structured_content["validated_count"] == 1
    receipt.update(all_passed=all(checks.values()), checks=checks, observations=observations)
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": checks, "receipt": str(output / "receipt.json")}, ensure_ascii=True), flush=True)
    if not all(checks.values()):
        raise RuntimeError("Acceptance failed; preserved receipt contains the evidence")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    asyncio.run(evaluate(parser.parse_args().tag))
