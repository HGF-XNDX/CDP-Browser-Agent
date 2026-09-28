"""Opt-in real 30000 collection -> durable dataset -> processing acceptance."""
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
from cdp_browser_agent.crawler.engine import Crawler
from cdp_browser_agent.context_budget import ContextBudget
from cdp_browser_agent.model_client import prepare_model_options
from cdp_browser_agent.processing.verification import verify_delivery
from examples.crawling.demo_site import demo_server


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def evaluate(tag):
    output = ROOT / "logs/live-crawler" / tag
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted((ROOT / "cdp_browser_agent").rglob("*.py")) + [Path(__file__), ROOT / "examples/crawling/demo_site.py"]
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sources:
            archive.write(path, path.relative_to(ROOT))
    config = load_config(str(ROOT / "examples/learning-30000.json"))
    config["crawler"].update(state_dir=str(output / "crawls"), request_delay_seconds=.1)
    config["agent"].update(log_dir=str(output / "runs"), max_steps=12,
        completion_processing=[{"profile": "titles", "min_records": 3}])
    config["web"].update(artifact_dir=str(output / "web"), allowed_private_hosts=["127.0.0.1"])
    config["processing"].update(artifact_dir=str(output / "processed"), replay_paths=[])
    profile = {"name": "titles", "description": "Extract the exact title from each supplied record", "instructions": "Return the original title field unchanged. Quote the original title as evidence.",
        "output_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False},
        "max_records": 30, "formats": ["json", "csv", "markdown"], "max_repairs": 1,
        "verification": [{"kind": "equals_input", "output": "title", "input": "title"}]}
    llm_profile = output / "titles.json"
    save(llm_profile, profile)
    config["processing"]["paths"] = [str(llm_profile)]
    save(output / "agent-config.json", config)
    budget = ContextBudget.from_settings(await prepare_model_options(config["model"]), config["agent"])
    receipt = {"scope": "Controlled static pagination, real CLI/MCP handoff, real model tool selection and per-record processing; not full-site/general benchmark accuracy.",
        "checks": {"capacity_262144": budget.context_window_tokens == 262144}, "context_budget": budget.as_dict(),
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
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
        with demo_server() as (server, url):
            spec = {"seed_urls": [url+"/list?page=1"], "max_depth": 0, "max_pages": 5,
                "item_selector": "article.item", "next_selector": "a.next", "fields": {"id": {"attribute": "data-id"}, "title": {"selector": "h2"}}, "key_fields": ["id"]}
            save(output / "spec.json", spec)
            mapping_path = output / "mapping.json"
            save(mapping_path, {**profile, "mode": "mapping", "field_map": {"title": "title"}})
            mcp_config = deepcopy(config)
            mcp_config["processing"]["paths"] = [str(mapping_path)]
            save(output / "mcp-config.json", mcp_config)
            process = await asyncio.create_subprocess_exec(sys.executable, "-X", "utf8", "-m", "cdp_browser_agent.browser", "--config", str(output / "mcp-config.json"),
                "--crawl-spec", str(output / "spec.json"), "--crawl-page-budget", "1", cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            (output / "cli-stderr.log").write_bytes(stderr)
            if process.returncode:
                raise RuntimeError("CLI failed; inspect cli-stderr.log")
            first = json.loads(stdout)
            receipt["cli"] = first
            receipt["checks"]["cli_checkpoint"] = first["status"] == "paused" and first["record_count"] == 2
            print(json.dumps({"phase": "cli_checkpoint", "status": first["status"]}), flush=True)
            with (output / "mcp-stderr.log").open("w", encoding="utf-8") as error_log:
                async with Client(stdio_client(StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "cdp_browser_agent.mcp_server", "--config", str(output / "mcp-config.json")], cwd=str(ROOT)), errlog=error_log)) as client:
                    receipt["checks"]["mcp_25_tools"] = len((await client.list_tools()).tools) == 25
                    result = await client.call_tool("web_crawl", {"crawl_id": first["crawl_id"]})
                    if result.is_error:
                        raise RuntimeError(str(result))
                    second = result.structured_content
                    receipt["mcp_crawl"] = second
                    receipt["checks"]["mcp_resumed_without_duplicate_request"] = second["ok"] and second["record_count"] == 3 and [p for p, _ in server.requests].count("/list?page=1") == 1
                    worker = await client.call_tool("browser_worker_start", {"profile": "titles", "crawl_id": first["crawl_id"]})
                    if worker.is_error:
                        raise RuntimeError(str(worker))
                    receipt["mcp_worker"] = worker.structured_content
                    receipt["checks"]["mcp_dataset_handoff"] = worker.structured_content["ok"] and worker.structured_content["validated_count"] == 3 and worker.structured_content["model_calls"] == 0
            task = (f"收集 {url}/list?page=1 的静态目录及下一页，按 id 去重，输出全部标题。"
                "已知页面模板：每条记录为 article.item，id 为该元素 data-id 属性，title 为其中 h2 的文本；下一页是 a.next 的 href。"
                "范围仅限列表分页，深度 0，最多 5 页。批量采集完后使用 titles 处理方法生成 JSON、CSV、Markdown，向我返回文件位置及记录数。")
            agent = await run_browser_agent(task, config)
            save(output / "agent-result.json", agent)
            names = [h["action"].get("name") for h in agent["history"] if h["action"]["action"] == "tool"]
            receipt["agent_tools"] = names
            receipt["checks"]["agent_selected_crawler_and_worker"] = "web_crawl" in names and "delegate_processing" in names
            receipt["checks"]["agent_completed_without_browser"] = agent["status"] == "completed" and not agent["browser_started"] and agent.get("completion_basis") == "host_verified"
            receipt["checks"]["answer_contains_all_fixture_titles"] = all(title in agent.get("answer", "") for title in ("Alpha", "Beta", "Gamma"))
            children = agent.get("processing_results", [])
            child = next((p for p in children if p.get("ok")), {})
            receipt["checks"]["real_worker_processed_three"] = child.get("validated_count") == 3 and child.get("model_calls", 0) >= 3
            if child:
                data = json.loads(Path(child["records_path"]).read_text(encoding="utf-8"))
                receipt["checks"]["exact_titles_and_export_hashes"] = [r["data"]["title"] for r in data] == ["Alpha", "Beta", "Gamma"] and verify_delivery(child, config["processing"]["artifact_dir"])["ok"]
            crawls = Crawler(config, parent_id=agent["run_id"]).list()
            receipt["agent_crawls"] = crawls
            receipt["checks"]["no_per_page_model_calls"] = bool(crawls) and all(c["model_calls"] == 0 and not c["browser_started"] for c in crawls)
            receipt["agent_metrics"] = agent.get("metrics", {})
            receipt["fixture_requests"] = server.requests
            print(json.dumps({"phase": "agent", "status": agent["status"], "tools": names}), flush=True)
        public = await Crawler(config).run(json.loads((ROOT / "examples/crawling/example-domain.json").read_text(encoding="utf-8")))
        receipt["public"] = public
        receipt["checks"]["public_example_domain"] = public["ok"] and public["record_count"] == 1
    finally:
        httpx.AsyncClient.send = real_send
        receipt["model_calls_traced"] = len(calls)
        receipt["ok"] = all(receipt["checks"].values())
        save(output / "receipt.json", receipt)
    print(json.dumps({"ok": receipt["ok"], "checks": receipt["checks"], "receipt": str(output / "receipt.json")}, ensure_ascii=False), flush=True)
    if not receipt["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    asyncio.run(evaluate(parser.parse_args().tag))
