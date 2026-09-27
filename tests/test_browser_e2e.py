"""Real Chromium + local HTTP model stub + real external MCP process.

This verifies wiring and browser outcomes, not an LLM's planning quality.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent


async def test_real_browser_skill_mcp_and_model_http(tmp_path):
    model_requests = []
    checks = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            html = b'''<html><head><title>Harness fixture</title></head><body>
            <h1>Page extraction fixture</h1><form onsubmit="event.preventDefault();document.getElementById('result').textContent='Verified '+document.getElementById('name').value;">
            <label>Name <input id="name" name="name"></label><button type="submit">Apply</button></form>
            <p id="result">Waiting for input</p></body></html>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            wire = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.loads(wire["messages"][-1]["content"])
            model_requests.append(payload)
            step = len(model_requests)
            if step == 1:
                action = {"action": "tool", "name": "skill_load", "arguments": {"name": "page-extraction"}}
            elif step == 2:
                checks.append(bool(payload["extensions"]["active_skills"]))
                action = {"action": "tool", "name": "skill_read", "arguments": {"name": "page-extraction", "path": "references/output.md"}}
            elif step == 3:
                checks.append("observed value" in json.dumps(payload["last_result"]))
                action = {"action": "tool", "name": "mcp.fixture.add", "arguments": {"a": 2, "b": 5}}
            elif step == 4:
                checks.append(payload["last_result"]["structured_content"]["sum"] == 7)
                target = next(e["id"] for e in payload["observation"]["elements"] if e.get("tag") == "input")
                action = {"action": "type", "target_id": target, "text": "Harness"}
            elif step == 5:
                action = {"action": "press", "key": "Enter"}
            elif step == 6:
                checks.append("Verified Harness" in payload["observation"]["fullText"])
                action = {"action": "save_page", "filename": "verified.txt"}
            else:
                checks.append(payload["last_result"]["ok"])
                action = {"action": "done", "outcome": "completed", "answer": "Verified Harness; external sum is 7; saved the observed page."}
            body = json.dumps({"choices": [{"message": {"content": json.dumps(action)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    config = browser_agent_default_config()
    config["web"]["prefer_fast_path"] = False
    config["model"].update(baseUrl=url + "/v1", model="fixture", provider="openai-compatible", maxRetries=0)
    config["browser"].update(headless=True, focus_page=False, start_url=url, downloads_path=str(tmp_path / "downloads"))
    config["agent"].update(max_steps=10, log_dir=str(tmp_path / "logs"))
    config["harness"].update(skill_paths=[str(Path(__file__).parents[1] / "examples" / "skills")],
                            mcp_servers={"fixture": {"command": sys.executable,
                                "args": [str(Path(__file__).parent / "fixtures" / "external_mcp.py")],
                                "allow_tools": ["add"]}})
    try:
        result = await run_browser_agent("Apply Harness, use the extraction skill and external add tool, then save the page.", config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result["status"] == "completed", result
    assert result["step"] == 7
    assert len(checks) == 5 and all(checks), checks
    assert len(result["collected_files"]) == 1
    assert "Verified Harness" in Path(result["collected_files"][0]).read_text(encoding="utf-8")
    assert [h["action"]["action"] for h in result["history"]] == ["tool", "tool", "tool", "type", "press", "save_page"]
