"""Opt-in real-30000 acceptance for durable processing and paired experience replay."""
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
from cdp_browser_agent.processing.sessions import ProcessingSessions
from cdp_browser_agent.processing.learning import ProcedureStore, replay_experience
from cdp_browser_agent.processing.verification import verify_delivery
from cdp_browser_agent.context_budget import ContextBudget
from cdp_browser_agent.model_client import prepare_model_options


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def title(result):
    return json.loads(Path(result["records_path"]).read_text(encoding="utf-8"))[0]["data"]["title"]


async def evaluate(tag):
    output = ROOT / "logs/live-workers" / tag
    output.mkdir(parents=True, exist_ok=False)
    source = sorted((ROOT / "cdp_browser_agent").rglob("*.py")) + sorted((ROOT / "examples/processing").glob("*.json")) + sorted((ROOT / "examples/replays").glob("*.json")) + [Path(__file__)]
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in source:
            archive.write(path, path.relative_to(ROOT))
    config = load_config(str(ROOT / "examples/learning-30000.json"))
    config["agent"].update(log_dir=str(output / "runs"))
    config["web"]["artifact_dir"] = str(output / "web")
    config["processing"]["artifact_dir"] = str(output / "processed")
    config_file = output / "config.json"
    save(config_file, config)
    budget = ContextBudget.from_settings(await prepare_model_options(config["model"]), config["agent"])
    receipt = {"checks": {"capacity_262144": budget.context_window_tokens == 262144}, "context_budget": budget.as_dict(),
               "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source},
               "scope": "Controlled section preference replay plus public example.com collection; not general semantic accuracy or a browser benchmark."}
    calls = []
    real_send = httpx.AsyncClient.send
    async def traced(client, request, **kwargs):
        response = await real_send(client, request, **kwargs)
        if request.method == "POST" and str(request.url).startswith(config["model"]["baseUrl"]):
            await response.aread()
            item = {"request": json.loads(request.content), "status": response.status_code, "response": response.json()}
            calls.append(item)
            with (output / "model-wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        return response
    httpx.AsyncClient.send = traced
    try:
        # First turn uses the real CLI process. Closing it cannot erase the worker.
        process = await asyncio.create_subprocess_exec(sys.executable, "-X", "utf8", "-m", "cdp_browser_agent.browser",
            "--config", str(config_file), "--worker-start", "section-heading", "--processing-input", str(ROOT / "examples/processing-inputs/heading.json"),
            cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        (output / "cli-stderr.log").write_bytes(stderr)
        if process.returncode:
            raise RuntimeError("CLI worker failed; inspect cli-stderr.log")
        first = json.loads(stdout)
        receipt["first"] = first
        first_folder = Path(first["records_path"]).parent
        old_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in first_folder.rglob("*") if p.is_file()}
        identity = first["worker_session_id"]
        receipt["checks"]["cli_initial_delivery"] = first["ok"] and title(first) == "Alpha Portal" and verify_delivery(first, config["processing"]["artifact_dir"])["ok"]
        print(json.dumps({"phase": "cli_first", "status": first["status"]}), flush=True)
        feedback = "后续采集只关心发布信息：请从 text 的 Releases 栏目下提取第一条发布标题，保持原文，不使用网站总标题。"
        with (output / "mcp-stderr.log").open("w", encoding="utf-8") as stderr_file:
            async with Client(stdio_client(StdioServerParameters(command=sys.executable,
                args=["-X", "utf8", "-m", "cdp_browser_agent.mcp_server", "--config", str(config_file)], cwd=str(ROOT)), errlog=stderr_file), read_timeout_seconds=240) as client:
                status = await client.call_tool("browser_worker_status", {"worker_session_id": identity})
                receipt["checks"]["mcp_reopened_cli_worker"] = status.structured_content["turn"] == 1 and status.structured_content["ok"]
                second_response = await client.call_tool("browser_worker_continue", {"worker_session_id": identity, "feedback": feedback, "expected_turn": 1})
                if second_response.is_error:
                    raise RuntimeError(str(second_response))
                second = second_response.structured_content
                receipt["second"] = second
        receipt["checks"]["same_worker_corrected"] = second["worker_session_id"] == identity and second["turn"] == 2 and title(second) == "Alpha 2.0 Released"
        receipt["checks"]["old_outputs_unchanged"] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in old_hashes.items())
        service = ProcessingSessions(config)
        state = service.status(identity)
        receipt["checks"]["restart_events_preserved"] = len([e for e in state["events"] if e["kind"] == "turn_finished"]) == 2
        receipt["checks"]["frozen_method_and_source"] = first["method_hash"] == second["method_hash"] and json.loads(Path(first["records_path"]).read_text(encoding="utf-8"))[0]["input_sha256"] == json.loads(Path(second["records_path"]).read_text(encoding="utf-8"))[0]["input_sha256"]
        print(json.dumps({"phase": "feedback", "status": second["status"]}), flush=True)
        replay = await replay_experience(config, second["candidate_experience_id"], "release-headings")
        receipt["replay"] = replay
        receipt["checks"]["paired_heldout_improvement"] = replay["promoted"] and all(c["candidate"]["passed"] and not c["baseline"]["passed"] for c in replay["cases"])
        print(json.dumps({"phase": "replay", "promoted": replay["promoted"]}), flush=True)
        fresh_record = {"data": {"title": "Delta Portal", "text": "Delta Portal\nNews\nWelcome\nReleases\nDelta 7 Released\nSupport\nHelp"}}
        child = await service.create("section-heading", [fresh_record])
        fresh = await service.run(child["worker_session_id"])
        receipt["fresh"] = fresh
        receipt["checks"]["fresh_worker_adopts_promoted_advice"] = title(fresh) == "Delta 7 Released" and fresh["experience_ids"] == [second["candidate_experience_id"]]
        with ProcedureStore(config) as store:
            store.revoke(second["candidate_experience_id"])
        child = await service.create("section-heading", [fresh_record])
        revoked = await service.run(child["worker_session_id"])
        receipt["revoked"] = revoked
        receipt["checks"]["revocation_stops_new_adoption"] = title(revoked) == "Delta Portal" and not revoked["experience_ids"]
        parent_config = deepcopy(config)
        parent_config["agent"]["completion_processing"] = [{"profile": "page-facts", "min_records": 1, "min_turn": 2}]
        parent = await run_browser_agent(
            "用 web_fetch 采集 https://example.com，再 delegate_processing 按 page-facts 加工，"
            "然后必须对同一个 worker_session_id 调用一次 processing_continue（feedback=将中文摘要压缩为一句话，保留来源引文；expected_turn 用刚返回的 turn）。"
            "检查修订后的成功结果，报告最终 JSON、CSV、Markdown 文件路径。只有修订完成后才能 done。无需浏览器。", parent_config)
        receipt["parent"] = parent
        names = [h["action"].get("name") for h in parent["history"]]
        results = parent.get("processing_results", [])
        receipt["checks"]["parent_collects_and_continues_child"] = all(n in names for n in ("web_fetch", "delegate_processing", "processing_continue")) and any(r.get("turn") == 2 and r["ok"] for r in results)
        receipt["checks"]["host_verified_parent_completion"] = parent["status"] == "completed" and parent.get("completion_basis") == "host_verified" and parent.get("verification", {}).get("ok") is True
        receipt["checks"]["parent_lineage_and_lazy_browser"] = not parent["browser_started"] and all(r["parent_run_id"] == parent["run_id"] for r in results)
        receipt["checks"]["replay_gold_not_in_worker_prompt"] = all("expected" not in json.loads(c["request"]["messages"][-1]["content"]) for c in calls if c["request"]["messages"][0]["content"].startswith("You are a data-processing"))
        print(json.dumps({"phase": "parent", "status": parent["status"], "tools": names}), flush=True)
    finally:
        httpx.AsyncClient.send = real_send
        receipt["direct_model_calls"] = len(calls)
        receipt["all_passed"] = len(receipt["checks"]) == 14 and all(receipt["checks"].values())
        save(output / "receipt.json", receipt)
    print(json.dumps({"checks": receipt["checks"], "all_passed": receipt["all_passed"], "receipt": str(output / "receipt.json")}), flush=True)
    if not receipt["all_passed"]:
        raise RuntimeError("Worker acceptance failed; inspect the preserved receipt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    asyncio.run(evaluate(parser.parse_args().tag))
