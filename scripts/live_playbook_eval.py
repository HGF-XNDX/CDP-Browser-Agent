"""Opt-in port-30000 acceptance: automatic learning, held-out adoption and retirement.

Fixtures measure section preferences and a local site's navigation convention. They
are deliberately controlled; this is not a real-web success-rate benchmark.
"""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.processing.sessions import ProcessingSessions
from cdp_browser_agent.processing.verification import verify_delivery
from cdp_browser_agent.harness.learning import LearningService
from cdp_browser_agent.harness.playbook import PlaybookStore, planner_method, scope
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.harness.tools import Tool
from cdp_browser_agent.model_client import prepare_model_options
from cdp_browser_agent.context_budget import ContextBudget
from cdp_browser_agent.mcp_server import create_mcp_server


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def record(name):
    return {"source_url": "https://example.com/"+name.lower(), "data": {"title": name+" Portal",
        "text": name+" Portal\nNews\nTeam gathering\nReleases\n"+name+" 7 Released\nSupport\nDocumentation"}}


def title(result):
    return json.loads(Path(result["records_path"]).read_text(encoding="utf-8"))[0]["data"]["title"]


async def evaluate(tag):
    output = ROOT / "logs/live-playbook" / tag
    output.mkdir(parents=True, exist_ok=False)
    source = sorted((ROOT / "cdp_browser_agent").rglob("*.py")) + sorted((ROOT / "examples/playbook-replays").glob("*.json")) + [ROOT / "examples/processing/playbook-heading.json", Path(__file__)]
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in source:
            archive.write(path, path.relative_to(ROOT))
    config = load_config(str(ROOT / "examples/playbook-30000.json"))
    config["agent"]["log_dir"] = str(output / "runs")
    config["processing"]["artifact_dir"] = str(output / "processed")
    config["learning"]["state_dir"] = str(output / "learning")
    save(output / "config.json", config)
    model = await prepare_model_options(config["model"])
    budget = ContextBudget.from_settings(model, config["agent"])
    receipt = {"checks": {"capacity_262144": budget.context_window_tokens == 262144}, "context_budget": budget.as_dict(),
        "model": model["model"], "scope": "Controlled preferences and planner/tool fixture; no general web performance claim.",
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source}}
    calls = []
    real_send = httpx.AsyncClient.send
    async def traced(client, request, **kwargs):
        response = await real_send(client, request, **kwargs)
        if request.method == "POST" and str(request.url).startswith(config["model"]["baseUrl"]):
            await response.aread()
            item = {"request": json.loads(request.content), "status": response.status_code, "response": response.json()}
            calls.append(item)
            with (output / "model-wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item, ensure_ascii=False)+"\n")
        return response
    httpx.AsyncClient.send = traced
    try:
        service = ProcessingSessions(config)
        child = await service.create("playbook-heading", [record("Alpha")])
        first = await service.run(child["worker_session_id"])
        receipt["first"] = first
        first_hash = hashlib.sha256(Path(first["records_path"]).read_bytes()).hexdigest()
        receipt["checks"]["baseline_default_heading"] = title(first) == "Alpha Portal"
        revised = await service.run(child["worker_session_id"], feedback="本发布信息采集工作流只提取 text 中 Releases 栏目下第一条发布标题，保留原文，不使用网站总标题或 News 栏目。", expected_turn=1)
        receipt["revised"] = revised
        learned = revised.get("playbook_learning", {})
        receipt["checks"]["automatic_reflector_and_curator"] = learned.get("status") == "completed" and bool(learned.get("delta", {}).get("operations"))
        receipt["checks"]["paired_heldout_improvement"] = bool(learned.get("replays")) and learned["replays"][0]["promoted"]
        receipt["checks"]["old_source_and_output_preserved"] = hashlib.sha256(Path(first["records_path"]).read_bytes()).hexdigest() == first_hash
        print(json.dumps({"phase": "worker_learning", "checks": receipt["checks"]}), flush=True)
        if not receipt["checks"]["paired_heldout_improvement"]:
            raise RuntimeError("Automatic lesson failed adoption; inspect receipt")
        learned_id = learned["candidates"][0]["id"]
        fresh = await service.create("playbook-heading", [record("Delta")])
        adopted = await service.run(fresh["worker_session_id"])
        receipt["adopted"] = adopted
        receipt["checks"]["unseen_worker_adopts_without_feedback"] = title(adopted) == "Delta 7 Released" and adopted["playbook_entries"] == [{"id": learned_id, "version": 1}]
        receipt["checks"]["fresh_artifact_contract"] = verify_delivery(adopted, config["processing"]["artifact_dir"])["ok"]
        async with Client(create_mcp_server(config)) as client:
            catalog = await client.list_tools()
            receipt["checks"]["mcp_30_tools"] = len(catalog.tools) == 30
            inspection = await client.call_tool("browser_playbook_read", {"entry_id": learned_id})
            receipt["checks"]["mcp_version_and_audit"] = inspection.structured_content["entry"]["state"] == "active" and bool(inspection.structured_content["events"])
            retired = await client.call_tool("browser_playbook_retire", {"entry_id": learned_id, "expected_version": 1})
            receipt["checks"]["mcp_retirement"] = retired.structured_content["state"] == "retired"
        fresh = await service.create("playbook-heading", [record("Epsilon")])
        after = await service.run(fresh["worker_session_id"])
        receipt["after_retirement"] = after
        receipt["checks"]["retirement_stops_adoption"] = title(after) == "Epsilon Portal" and not after["playbook_entries"]
        # Planner adoption uses a real registered fixture tool, not a replacement planner.
        # The catalog intentionally has a site-specific legacy default; its error is
        # environment feedback. Expected replay values never enter learning prompts.
        planner_config = deepcopy(config)
        planner_config["learning"].update(task_type="catalog-collection", auto_replay=False)
        async def catalog_fetch(channel="latest"):
            if channel == "archive":
                return {"ok": True, "url": "https://catalog.example", "records": ["A", "B", "C"], "complete": True}
            return {"ok": False, "url": "https://catalog.example", "errorType": "legacy_preview",
                    "message": "此站 latest 是旧版预览，只有 1 条。archive 通道提供本期完整 3 条记录。"}
        tool = Tool("catalog_fetch", "读取目录记录。channel 可为 latest 或 archive，默认 latest。",
                    {"type": "object", "properties": {"channel": {"enum": ["latest", "archive"]}}, "additionalProperties": False}, catalog_fetch)
        training = await run_browser_agent("使用 catalog_fetch 收集 https://catalog.example 的本期完整目录，得到全部记录后直接报告。", planner_config, tools=[tool])
        receipt["planner_training"] = training
        learned_planner = training.get("playbook_learning", {})
        receipt["checks"]["planner_autolearns_from_actual_failure"] = bool(learned_planner.get("candidates")) and any(h["result"].get("ok") is False for h in training["history"])
        if not receipt["checks"]["planner_autolearns_from_actual_failure"]:
            raise RuntimeError("Planner training did not produce a grounded candidate")
        suite = {"name": "catalog-routing", "target": "planner", "task_type": "catalog-collection", "host": "catalog.example",
            "cases": [{"task": t, "observation": {"url": "https://catalog.example", "elements": [], "fullText": "目录服务。使用 catalog_fetch 工具读取数据。"},
                       "last_result": {"ok": True, "data": ExtensionRuntime(planner_config, [tool]).registry.describe("catalog_fetch")},
                       "browser_started": False, "expected": {"action": "tool", "name": "catalog_fetch", "arguments": {"channel": "archive"}}}
                      for t in ("获取目录服务的本期全部条目。", "请读取当前批次的完整目录数据。") ]}
        suite_path = output / "planner-suite.json"
        save(suite_path, suite)
        planner_config["learning"]["replay_paths"] = [str(suite_path)]
        async with ExtensionRuntime(planner_config, [tool]) as runtime:
            candidate = learned_planner["candidates"][0]
            replay = await LearningService(planner_config).replay(candidate["id"], candidate["version"], "catalog-routing", runtime=runtime)
        receipt["planner_replay"] = replay
        receipt["checks"]["planner_paired_improvement"] = replay["promoted"]
        fresh_planner = await run_browser_agent("使用 catalog_fetch 读取 https://catalog.example 本期所有条目，完成后报告。", planner_config, tools=[tool])
        receipt["planner_adoption"] = fresh_planner
        actions = [h for h in fresh_planner["history"] if h["action"].get("name") == "catalog_fetch"]
        receipt["checks"]["fresh_planner_uses_matching_advice"] = bool(fresh_planner.get("playbook_selections", [{}])[0].get("entries")) and bool(actions) and actions[0]["action"]["arguments"].get("channel") == "archive" and actions[0]["result"]["complete"]
        generator_payloads = [json.loads(c["request"]["messages"][-1]["content"]) for c in calls
                              if c["request"]["messages"][0]["content"].startswith(("You are a data-processing", "You are a general-purpose"))]
        receipt["checks"]["heldout_gold_not_in_generator_prompts"] = all("expected" not in p and "cases" not in p for p in generator_payloads)
        receipt["checks"]["no_browser_needed_for_fixture"] = not training["browser_started"] and not fresh_planner["browser_started"]
    finally:
        httpx.AsyncClient.send = real_send
        receipt["model_calls"] = len(calls)
        receipt["usage"] = {k: sum(c["response"].get("usage", {}).get(k, 0) for c in calls) for k in ("prompt_tokens", "completion_tokens")}
        receipt["all_passed"] = len(receipt["checks"]) == 16 and all(receipt["checks"].values())
        save(output / "receipt.json", receipt)
    print(json.dumps({"all_passed": receipt["all_passed"], "checks": receipt["checks"], "model_calls": len(calls), "receipt": str(output / "receipt.json")}), flush=True)
    if not receipt["all_passed"]:
        raise RuntimeError("Live playbook acceptance failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    asyncio.run(evaluate(parser.parse_args().tag))
