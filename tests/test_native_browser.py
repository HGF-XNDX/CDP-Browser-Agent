"""Actual Chromium contracts, independent of a planner's claims."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cdp_browser_agent.browser.controller import BrowserController
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.policy import validate_action


@pytest.fixture
async def browser(tmp_path):
    config = browser_agent_default_config()
    config["browser"].update(headless=True, focus_page=False, downloads_path=str(tmp_path))
    controller = await BrowserController.launch(config)
    try:
        yield controller
    finally:
        await controller.close()


async def target(browser, **attributes):
    obs = await browser.observe()
    return obs, next(e["id"] for e in obs["elements"] if all(e.get(k) == v for k, v in attributes.items()))


async def test_native_form_controls_and_trusted_click(browser):
    await browser.page.set_content('''<form onsubmit="event.preventDefault();window.saved = Object.fromEntries(new FormData(this))">
        <input name="name"><select name="region"><option value="">Choose</option>
        <option value="east">East</option><option value="west" disabled>West</option></select>
        <input name="newsletter" type="checkbox" value="yes">
        <button onclick="window.trusted = event.isTrusted">Save</button></form>''')
    obs, tid = await target(browser, tag="input", name="name")
    assert (await browser.execute(validate_action({"action": "type", "target_id": tid, "text": "Lin"}, obs)))["ok"]
    obs, tid = await target(browser, tag="select")
    assert {"value": "east", "label": "East", "selected": False, "disabled": False} in next(e for e in obs["elements"] if e["id"] == tid)["options"]
    with pytest.raises(ValueError, match="enabled option"):
        validate_action({"action": "select_option", "target_id": tid, "value": "west"}, obs)
    assert (await browser.execute(validate_action({"action": "select_option", "target_id": tid, "label": "East"}, obs)))["values"] == ["east"]
    obs, tid = await target(browser, tag="input", type="checkbox")
    for _ in range(2):
        assert (await browser.execute(validate_action({"action": "set_checked", "target_id": tid, "checked": True}, obs)))["checked"]
    obs, tid = await target(browser, tag="button")
    assert (await browser.execute(validate_action({"action": "click", "target_id": tid}, obs)))["ok"]
    assert await browser.page.evaluate("window.saved") == {"name": "Lin", "region": "east", "newsletter": "yes"}
    assert await browser.page.evaluate("window.trusted") is True


async def test_frame_targets_are_scoped_and_detached_targets_fail(browser):
    await browser.page.set_content('''<button>Outer</button><iframe srcdoc="&lt;button onclick='window.activated = event.isTrusted'&gt;Inner&lt;/button&gt;"></iframe>''')
    await browser.page.frames[1].wait_for_selector("button")
    obs, tid = await target(browser, text="Inner", tag="button")
    assert tid.startswith("frame_1:")
    assert "Inner" in obs["fullText"] and obs["frames"]
    assert (await browser.execute(validate_action({"action": "click", "target_id": tid}, obs)))["ok"]
    assert await browser.page.frames[1].evaluate("window.activated") is True
    obs, tid = await target(browser, text="Outer", tag="button")
    await browser.page.evaluate("document.querySelector('button').outerHTML = '<button>Replacement</button>'")
    with pytest.raises(ValueError, match="detached"):
        await browser.execute({"action": "click", "target_id": tid})


async def test_cookie_download_and_click_artifact(browser):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/report.csv":
                ok = self.headers.get("Cookie") == "session=ok"
                self.send_response(200 if ok else 403)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", 'attachment; filename="report.csv"')
                body = b"sku,quantity\nA17,8\n" if ok else b"Forbidden"
            else:
                self.send_response(200)
                self.send_header("Set-Cookie", "session=ok; HttpOnly; Path=/")
                self.send_header("Content-Type", "text/html")
                body = b'<a href="/report.csv" download>Download</a>'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        # Unauthenticated requests must still fail; no forged Cookie header.
        assert not (await browser.download_url(url + "/report.csv"))["ok"]
        await browser.page.goto(url)
        direct = await browser.download_url(url + "/report.csv")
        assert direct["ok"] and Path(direct["path"]).read_bytes() == b"sku,quantity\nA17,8\n"
        obs, tid = await target(browser, tag="a")
        clicked = await browser.execute(validate_action({"action": "click", "target_id": tid}, obs))
        assert clicked["ok"] and Path(clicked["path"]).read_bytes() == b"sku,quantity\nA17,8\n"
        assert clicked["path"] != direct["path"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_text_save_is_not_mislabelled_html(browser):
    await browser.page.set_content("<h1>A page</h1>")
    saved = await browser.save_current_page("snapshot.html")
    assert saved["ok"] and Path(saved["path"]).suffix == ".txt"
