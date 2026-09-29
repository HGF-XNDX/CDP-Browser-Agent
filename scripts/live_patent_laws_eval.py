"""Opt-in real-model + real-official-web + external stdio MCP acceptance."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import zipfile
import base64
import xml.etree.ElementTree as ET

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.browser.runner import run_browser_agent
from examples.patent_laws.collector import PatentCollection, sha
from cdp_browser_agent.workflows.store import atomic_json


async def evaluate(tag, asof):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tag):
        raise ValueError("Use a simple unique run tag")
    log = ROOT / "logs/live-patent-laws" / tag
    log.mkdir(parents=True, exist_ok=False)
    output = ROOT / "deliveries/patent-laws" / tag
    if output.exists():
        raise ValueError("Use a new tag; prior evidence is immutable")
    config = load_config(str(ROOT / "examples/patent-laws-30000.json"))
    config["agent"]["log_dir"] = str(log / "agent")
    config["harness"].update(state_dir=str(log / "state"), artifact_dir=str(log / "artifacts"))
    config["web"]["artifact_dir"] = str(log / "web")
    config["processing"]["artifact_dir"] = str(log / "parent-workers")
    server = config["harness"]["mcp_servers"]["patent-laws"]
    server.update(command=sys.executable, cwd=str(ROOT), args=["-m", "examples.patent_laws.collector",
        "--output", str(output), "--as-of", asof],
        env_from=[name for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY") if name in os.environ])
    server["env"] = {"PYTHONUTF8": "1"}
    atomic_json(log / "config.json", config)
    sources = sorted((ROOT / "cdp_browser_agent").rglob("*.py")) + sorted((ROOT / "examples/patent_laws").glob("*.py")) + [
        Path(__file__), ROOT / "examples/patent_laws/article-copy.json",
        ROOT / "examples/patent-laws-30000.json", ROOT / "examples/skills/patent-law-collection/SKILL.md"]
    hashes = {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in sources}
    with zipfile.ZipFile(log / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for p in sources:
            archive.write(p, p.relative_to(ROOT))
    wire = []
    send = httpx.AsyncClient.send
    async def traced(client, request, **kwargs):
        response = await send(client, request, **kwargs)
        if str(request.url).startswith(config["model"]["baseUrl"]) and request.method == "POST":
            await response.aread()
            record = {"request": json.loads(request.content), "response": response.json()}
            wire.append(record)
            with (log / "model-wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return response
    httpx.AsyncClient.send = traced
    try:
        task = "从网上下载中国、美国、日本各一部专利法，分别输出每部法律一个JSON对象，articles数组每条一个元素。保留原文、款项、来源及版本信息。使用已配置的patent-law-collection方法，先发现MCP工具，完成三个国家的下载与逐条导出，最后调用status核验完整性。不要把目录、历史注释或重复条号当成新的普通条文；保留删除、废止、改号与附则标记。最后给出实际文件路径、数量和版本局限。"
        state = await run_browser_agent(task, config)
    finally:
        httpx.AsyncClient.send = send
    atomic_json(log / "agent-state.json", state)
    collection = PatentCollection(config, output, asof)
    status = collection.status()
    actions = [h["action"] for h in state.get("history", [])]
    calls = [a for a in actions if a["action"] == "tool"]
    export_countries = {a.get("arguments", {}).get("country") for a in calls if a["name"].endswith("patent_articles_export")}
    fetch_countries = {a.get("arguments", {}).get("country") for a in calls if a["name"].endswith("patent_source_fetch")}
    checks = {"agent_completed": state["status"] == "completed", "all_files_source_hash_verified": status["ok"],
        "agent_fetched_all_three": fetch_countries == {"CN", "US", "JP"},
        "agent_exported_all_three": export_countries == {"CN", "US", "JP"},
        "agent_requested_final_status": any(a["name"].endswith("patent_collection_status") for a in calls),
        "no_browser_for_static_sources": not state["browser_started"],
        "source_code_unchanged": all(sha((ROOT / p).read_bytes()) == v for p, v in hashes.items())}
    laws = []
    for country in ("CN", "US", "JP"):
        path = output / country / "law.json"
        if not path.exists():
            continue
        law = json.loads(path.read_text(encoding="utf-8"))
        laws.append(law)
        checks[f"{country}_every_article_hash"] = all(sha(a["text"]) == a["text_sha256"] for a in law["articles"])
        if country == "JP":
            source = json.loads((output / law["source"]["raw_path"]).read_bytes())
            xml = ET.fromstring(base64.b64decode(source["law_full_text"]))
            tables = xml.findall("./LawBody/AppdxTable")
            appendix_tables = [a for a in law.get("appendices", []) if a["element"] == "AppdxTable"]
            checks["JP_all_appendix_tables"] = bool(tables) and len(tables) == len(appendix_tables) and all(
                ''.join(t.strip() for t in original.itertext()) == saved["text"]
                for original, saved in zip(tables, appendix_tables))
        # Exercise restart/idempotence without another web or model call.
        before = sha(path.read_bytes())
        result = await collection.export(country)
        checks[f"{country}_resume_exact_output"] = result["ok"] and sha(path.read_bytes()) == before
    atomic_json(output / "laws.json", {"schema_version": 1, "laws": laws})
    with (output / "articles.jsonl").open("w", encoding="utf-8") as stream:
        for law in laws:
            for a in law["articles"]:
                stream.write(json.dumps({"country": law["country"], "law_title": law["title"], **a}, ensure_ascii=False) + "\n")
    receipt = {"all_passed": all(checks.values()), "checks": checks, "agent_status": state["status"],
        "steps": state["step"], "model_calls": len(wire), "metrics": state.get("metrics"),
        "source_sha256": hashes, "delivery": status, "actions": actions,
        "scope": "Official source snapshots; deterministic structural completeness and copying; not a legal opinion or newest-law guarantee."}
    atomic_json(log / "receipt.json", receipt)
    print(json.dumps({"all_passed": receipt["all_passed"], "checks": checks, "steps": state["step"],
        "model_calls": len(wire), "countries": [{k:r.get(k) for k in ["country", "status", "article_count", "main_article_count"]} for r in status["countries"]],
        "receipt": str(log / "receipt.json")}, ensure_ascii=False), flush=True)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--as-of", default="2026-09-30")
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.tag, args.as_of))
    raise SystemExit(0 if result["all_passed"] else 1)
