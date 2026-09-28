"""Opt-in real-model acceptance for capacity discovery, spill and recoverable pruning."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
import zipfile

import httpx
from mcp import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.context_budget import ContextBudget
from cdp_browser_agent.model_client import prepare_model_options
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.harness.artifacts import ArtifactStore
from cdp_browser_agent.harness.compaction import ContextCompactor
from cdp_browser_agent.harness.tools import Tool
from cdp_browser_agent.mcp_server import create_mcp_server


async def evaluate(tag):
    folder = ROOT / "logs/live-context" / tag
    folder.mkdir(parents=True, exist_ok=False)
    source = sorted((ROOT / "cdp_browser_agent").rglob("*.py")) + [Path(__file__)]
    with zipfile.ZipFile(folder / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in source:
            archive.write(path, path.relative_to(ROOT))
    config = load_config(str(ROOT / "examples/harness-30000.json"))
    config["agent"].update(log_dir=str(folder / "runs"), max_steps=12)
    config["harness"].update(run_timeout_seconds=240, max_tool_result_chars=60000)
    config["processing"]["paths"] = []
    model = await prepare_model_options(config["model"])
    budget = ContextBudget.from_settings(model, config["agent"])
    receipt = {"checks": {}, "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source},
               "detected_budget": budget.as_dict(), "model": model["model"]}
    receipt["checks"]["actual_capacity_262144"] = budget.model_capacity_tokens == budget.context_window_tokens == 262144
    nonce = "proof-" + uuid4().hex
    evidence = {"ok": True, "source": "controlled acceptance fixture; not a public website",
                "text": "普通采集材料，无目标记录。" * 10000 + "\n验收记录：" + nonce}
    invocations = 0
    async def fixture():
        nonlocal invocations
        invocations += 1
        return evidence
    # Exercise compression with an intentional lower operator limit. This does
    # not change the model server's real 262K allocation.
    limited = deepcopy(config)
    limited["agent"]["context_window_tokens"] = 14000
    wire = []
    send = httpx.AsyncClient.send
    async def traced(client, request, **kwargs):
        response = await send(client, request, **kwargs)
        if request.method == "POST" and str(request.url).startswith(config["model"]["baseUrl"]):
            await response.aread()
            event = {"request": json.loads(request.content), "status": response.status_code, "response": response.json()}
            wire.append(event)
            with (folder / "model-wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return response
    httpx.AsyncClient.send = traced
    try:
        result = await run_browser_agent(
            "调用已注册的 fixture_records 工具一次，获取采集材料，找到正文最后的‘验收记录：’并原样报告后面的 proof 标识。"
            "如果工具结果或上下文被压缩，使用 artifact_search 查找‘验收记录’，必要时 artifact_read 按偏移回读。"
            "不要重复采集，不要猜测标识，不需要上网或启动浏览器。", limited,
            tools=[Tool("fixture_records", "Return a large collected document with a final acceptance record.", {"type": "object", "properties": {}, "additionalProperties": False}, fixture, read_only=True)])
        receipt["task"] = result
        names = [h["action"].get("name") for h in result["history"]]
        receipt["checks"].update(
            real_model_recovered_tail=nonce in result["answer"] and result["status"] == "completed",
            evidence_collected_once=invocations == 1,
            model_uses_reference_tools=any(n in names for n in ("artifact_search", "artifact_read")),
            no_browser=result["browser_started"] is False,
            soft_limit_preserved=result.get("context_budget", {}).get("context_window_tokens") == 14000)
        print(json.dumps({"status": result["status"], "tools": names, "checks": receipt["checks"]}), flush=True)
        run_dir = folder / "runs/sessions" / result["run_id"]
        commits = [json.loads(p.read_text(encoding="utf-8")) for p in (run_dir / "compactions").glob("*.json")]
        commits = [c for c in commits if c["status"] == "committed"]
        receipt["checks"]["live_compaction_committed"] = bool(commits)
        tool_result = next(h["result"] for h in result["history"] if h["action"].get("name") == "fixture_records")
        identity = tool_result["artifact"]["artifact_id"]
        reopened = ArtifactStore(run_dir / "artifacts")
        receipt["checks"]["restart_full_result_identical"] = reopened.load(identity) == evidence
        async with Client(create_mcp_server(limited)) as client:
            found = await client.call_tool("browser_artifact_search", {"run_id": result["run_id"], "artifact_id": identity, "query": "验收记录"})
            offset = found.structured_content["matches"][0]["offset"]
            read = await client.call_tool("browser_artifact_read", {"run_id": result["run_id"], "artifact_id": identity, "offset": offset, "limit": 500})
            receipt["checks"]["mcp_reads_saved_original"] = nonce in read.structured_content["text"]
            invalid = await client.call_tool("browser_artifact_read", {"run_id": "../outside", "artifact_id": identity})
            receipt["checks"]["mcp_rejects_path_traversal"] = invalid.is_error
        if commits:
            commit = commits[0]
            receipt["checks"]["committed_projection_verified"] = (
                commit["after_tokens"] < commit["before_tokens"] and
                bool(reopened.load(commit["original"]["artifact_id"])) and
                bool(reopened.load(commit["projection"]["artifact_id"])))
        receipt["model_requests"] = len(wire)
    finally:
        httpx.AsyncClient.send = send
        receipt["all_passed"] = bool(receipt["checks"]) and all(receipt["checks"].values())
        (folder / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": receipt["checks"], "receipt": str(folder / "receipt.json")}, ensure_ascii=True), flush=True)
    if not receipt["all_passed"]:
        raise RuntimeError("Acceptance failed; inspect the preserved receipt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    asyncio.run(evaluate(parser.parse_args().tag))
