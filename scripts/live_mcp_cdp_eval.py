"""Opt-in real-model acceptance through a subprocess MCP server and isolated CDP.

Creates only a dedicated headless Chromium profile. Never attaches to a user's
existing browser or overwrites an earlier receipt. Uses live_eval's iframe fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from playwright.async_api import async_playwright

from live_eval import ROOT, Fixture
from cdp_browser_agent.browser.default_config import browser_agent_default_config


async def main(args):
    output = ROOT / "logs/live-eval" / args.tag
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    browser_process = None
    began = time.perf_counter()
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
            discovery = (await http.get(args.base_url.rstrip("/") + "/models")).json()
            model = discovery["data"][0]["id"]
            async with async_playwright() as pw:
                browser_process = subprocess.Popen([
                    pw.chromium.executable_path, "--headless", "--no-first-run", "--no-default-browser-check",
                    f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={output / 'isolated-profile'}", "about:blank"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                for _ in range(50):
                    try:
                        response = await http.get(endpoint + "/json/version", timeout=.5)
                        response.raise_for_status()
                        break
                    except (httpx.HTTPError, OSError):
                        await asyncio.sleep(.1)
                else:
                    raise RuntimeError("Isolated CDP browser did not become available")
            config = browser_agent_default_config()
            config["model"].update(baseUrl=args.base_url, model=model, maxTokens=1536, apiTimeout=90, maxRetries=0)
            config["browser"].update(connection="cdp", cdp_url=endpoint, auto_start_cdp=False, focus_page=False,
                start_url=f"http://127.0.0.1:{fixture.server_port}/frame", downloads_path=str(output / "downloads"))
            config["agent"].update(max_steps=8, log_dir=str(output / "events"))
            config["harness"]["run_timeout_seconds"] = 180
            config_path = output / "config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "source-sha256.json").write_text(json.dumps({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT / "cdp_browser_agent").rglob("*.py"))}, indent=2), encoding="utf-8")
            with (output / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
                async with Client(stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "cdp_browser_agent.mcp_server",
                    "--config", str(config_path)], cwd=str(ROOT)), errlog=stderr), read_timeout_seconds=200) as client:
                    capabilities = await client.call_tool("browser_capabilities", {})
                    result = await client.call_tool("browser_task", {"task": "点击嵌入设置面板中的 Activate panel 按钮，确认主页面显示 Frame activated。", "max_steps": 8})
            browser_survived = browser_process.poll() is None and (await http.get(endpoint + "/json/version")).status_code == 200
            result_data = result.structured_content
            receipt = {"model": model, "base_url": args.base_url, "transport": "stdio", "connection": "cdp",
                "capabilities": capabilities.structured_content, "tool_error": result.is_error,
                "result": result_data, "fixture_activated": Fixture.activated,
                "attached_browser_preserved": browser_survived, "elapsed": round(time.perf_counter()-began, 3),
                "checked_success": not result.is_error and result_data.get("status") == "completed" and Fixture.activated and browser_survived}
            (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(receipt, ensure_ascii=True), flush=True)
    finally:
        if browser_process is not None and browser_process.poll() is None:
            browser_process.terminate()
            try:
                browser_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser_process.kill()
                browser_process.wait(timeout=5)
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    asyncio.run(main(parser.parse_args()))
