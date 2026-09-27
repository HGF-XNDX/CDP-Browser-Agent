"""Bounded real-model browser evaluation; never part of automatic pytest runs."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.browser.controller import BrowserController
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent


PAGES = {
    "/catalog": """<title>Supply catalog</title><h1>Office supplies</h1><table><tr><th>SKU</th><th>Name</th><th>Price CNY</th><th>Stock</th></tr>
    <tr><td>A17</td><td>Desk lamp</td><td>129</td><td>8</td></tr><tr><td>B28</td><td>Desk lamp Pro</td><td>179</td><td>12</td></tr>
    <tr><td>C39</td><td>Budget lamp</td><td>89</td><td>0</td></tr></table>""",
    "/form": """<title>Local contact preferences</title><h1>Contact preferences</h1>
    <form onsubmit="event.preventDefault();let f=new FormData(this);document.querySelector('#result').textContent=JSON.stringify(Object.fromEntries(f));fetch('/record?'+new URLSearchParams(f))">
    <label>Name <input name="name" required></label><label>Email <input name="email" type="email" required></label>
    <label>Region <select name="region"><option value="">Choose region</option><option value="east">East</option><option value="west">West</option></select></label>
    <label><input type="checkbox" name="newsletter" value="yes">Receive newsletter</label><button type="submit">Save preferences</button></form><pre id="result">Not saved</pre>""",
    "/text": """<title>Text normalization source</title><h1>Normalize this phrase</h1><pre>  blue    river   quiet     morning  </pre>""",
    "/private": """<title>Session file</title><h1>Download report</h1><a href="/protected.csv" download="report.csv">Download CSV</a><p>This file requires the session cookie set by this page.</p>""",
    "/frame": """<title>Embedded panel</title><h1>Embedded status panel</h1><iframe title="Settings" src="/inside"></iframe><p id="result">Not activated</p>
    <script>addEventListener('message',e=>{if(e.data==='activated'){document.querySelector('#result').textContent='Frame activated';fetch('/activated')}})</script>""",
    "/inside": """<title>Settings frame</title><button onclick="parent.postMessage('activated','*')">Activate panel</button>""",
}


class Fixture(BaseHTTPRequestHandler):
    records: list[dict] = []
    downloads = 0
    activated = False

    def log_message(self, *_):
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        status, kind = 200, "text/html; charset=utf-8"
        if path == "/record":
            Fixture.records.append(parse_qs(urlparse(self.path).query))
            text = "saved"
        elif path == "/activated":
            Fixture.activated = True
            text = "activated"
        elif path == "/protected.csv":
            if "fixture_session=allowed" not in self.headers.get("Cookie", ""):
                status, text = 403, "Forbidden: session cookie required"
            else:
                Fixture.downloads += 1
                text, kind = "sku,quantity\nA17,8\n", "text/csv"
        else:
            text = PAGES.get(path, "Not found")
            status = 200 if path in PAGES else 404
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if path == "/private":
            self.send_header("Set-Cookie", "fixture_session=allowed; Path=/; SameSite=Lax")
        if path == "/protected.csv" and status == 200:
            self.send_header("Content-Disposition", 'attachment; filename="report.csv"')
        self.end_headers()
        self.wfile.write(body)


async def evaluate(args):
    output = (ROOT / "logs" / "live-eval" / args.tag).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_paths = sorted((ROOT / "cdp_browser_agent").rglob("*.py"))
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    with zipfile.ZipFile(output / "source-snapshot.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in source_paths:
            z.write(p, p.relative_to(ROOT))
        z.write(Path(__file__), "scripts/live_eval.py")
    with httpx.Client(timeout=10, trust_env=False) as client:
        models = client.get(args.base_url.rstrip("/") + "/models").json()
    model = args.model or models["data"][0]["id"]
    metadata = {"started": datetime.now(timezone.utc).isoformat(), "base_url": args.base_url,
                "model": model, "model_discovery": models, "source_sha256": hashes,
                "test_kind": "real_model_bounded_engineering_smoke", "not_a_benchmark": True}
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    skill = output / "skills" / "text-normalization"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('''---
name: text-normalization
description: Normalize webpage phrases and report word counts using the configured text processing capability. Use when asked for text normalization.
---
Read the phrase from the actual page. Discover the external normalize_text tool,
inspect its arguments, and use it to normalize whitespace and count words.
Do not invent a tool result or count manually. Return the normalized text, words,
and source URL. If the tool is unavailable, report the missing capability.
''', encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    cases = {
        "read": ("/catalog", "从这个商品页面选出库存充足、价格不超过150元的最便宜台灯，给出 SKU、价格、库存和来源。"),
        "form": ("/form", "在这个本地测试表单中把 Name 填为 Lin，Email 填为 lin@example.test，Region 选择 East，勾选 Receive newsletter，然后保存并确认保存结果。"),
        "skill_mcp": ("/text", "按可用的文本整理流程，规范化页面中的短语，并给出规范化文本、词数和来源。"),
        "download": ("/private", "下载这个页面提供的 CSV 报告到本地，确认文件保存成功，告诉我文件路径。"),
        "iframe": ("/frame", "点击嵌入设置面板中的 Activate panel 按钮，确认主页面显示 Frame activated。"),
        "public": ("https://example.com", "打开并读取这个公开页面，给出页面标题和主要用途，并附来源网址。"),
    }
    all_results = []
    real_send = httpx.AsyncClient.send
    telemetry_path = None
    counts = []

    async def traced_send(client, request, **kwargs):
        began = time.perf_counter()
        try:
            response = await real_send(client, request, **kwargs)
        except BaseException as exc:
            if str(request.url).startswith(args.base_url) and request.method == "POST":
                with telemetry_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"elapsed": time.perf_counter()-began, "error": type(exc).__name__}) + "\n")
            raise
        if str(request.url).startswith(args.base_url) and request.method == "POST":
            await response.aread()
            try:
                data = response.json()
            except ValueError:
                data = {"body": response.text}
            record = {"elapsed": time.perf_counter()-began, "status": response.status_code,
                      "request": json.loads(request.content), "response": data}
            counts.append({"elapsed": record["elapsed"], "status": record["status"], "usage": data.get("usage", {})})
            with telemetry_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return response

    httpx.AsyncClient.send = traced_send
    try:
        for name in args.cases.split(","):
            path, task = cases[name]
            case_dir = output / name
            case_dir.mkdir()
            telemetry_path = case_dir / "model-wire.jsonl"
            counts = []
            config = browser_agent_default_config()
            config["model"].update(baseUrl=args.base_url, model=model, provider="llama.cpp", maxTokens=1536, apiTimeout=90, maxRetries=0, answerLanguage="zh")
            config["browser"].update(start_url=path if path.startswith("http") else root+path,
                                     headless=True, focus_page=False, downloads_path=str(case_dir / "downloads"))
            config["agent"].update(max_steps=args.max_steps, log_dir=str(case_dir / "events"), browser_site_memory_enabled=False,
                                   strategy_evaluator_enabled=False, memory_use_model_summaries=False)
            config["harness"].update(run_timeout_seconds=180, skill_paths=[str(skill.parent)], mcp_servers={"text": {
                "command": sys.executable, "args": [str(ROOT / "examples/toolbox.py")], "allow_tools": ["normalize_text"]}})
            (case_dir / "config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"event":"case_start","case":name,"model":model}), flush=True)
            began = time.perf_counter()
            try:
                result = await run_browser_agent(task, config)
            except Exception as exc:
                result = {"status": "exception", "answer": repr(exc)}
            duration = time.perf_counter()-began
            history = result.get("history", [])
            files = [Path(h.get("result", {}).get("path", "")) for h in history if h.get("result", {}).get("ok") and h.get("result", {}).get("path")]
            observed = " ".join(s.get("snippet", "") for s in result.get("sources", []))
            answer = result.get("answer", "")
            if name == "read":
                checked = all(token in answer for token in ("A17", "129", "8")) and "A17" in observed
            elif name == "form":
                checked = any(r == {"name":["Lin"],"email":["lin@example.test"],"region":["east"],"newsletter":["yes"]} for r in Fixture.records)
            elif name == "skill_mcp":
                called = [h for h in history if h.get("action", {}).get("name") == "mcp.text.normalize_text" and h.get("result", {}).get("ok")]
                loaded = any(h.get("action", {}).get("name") == "skill_load" and h.get("result", {}).get("ok") for h in history)
                checked = bool(called) and loaded and "blue river quiet morning" in answer and "4" in answer
            elif name == "download":
                checked = any(p.is_file() and "A17,8" in p.read_text(encoding="utf-8") for p in files)
            elif name == "iframe":
                checked = Fixture.activated
            else:
                checked = "Example Domain" in observed and any(s.get("url", "").startswith("https://example.com") for s in result.get("sources", [])) and "example.com" in answer
            summary = {"case": name, "status":result.get("status"), "checked_success":bool(checked),
                       "steps":result.get("step"), "elapsed":round(duration,3), "model_calls":len(counts),
                       "prompt_tokens":sum(c["usage"].get("prompt_tokens",0) for c in counts),
                       "completion_tokens":sum(c["usage"].get("completion_tokens",0) for c in counts),
                       "actions":[h.get("action") for h in history], "answer":answer,
                       "action_errors":[h.get("result") for h in history if not h.get("result",{}).get("ok")],
                       "files":[str(p) for p in files]}
            (case_dir / "result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
            (case_dir / "summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
            all_results.append(summary)
            (output / "summary.json").write_text(json.dumps(all_results,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"event":"case_end",**summary},ensure_ascii=False), flush=True)
    finally:
        httpx.AsyncClient.send = real_send
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print(str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    parser.add_argument("--model")
    parser.add_argument("--cases", default="read,form,skill_mcp,download,iframe,public")
    parser.add_argument("--max-steps", type=int, default=12)
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(evaluate(parser.parse_args()))
