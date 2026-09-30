import asyncio
import hashlib
import gzip
import json
from pathlib import Path
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import Client

from cdp_browser_agent.browser import agent
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.browser.policy import validate_action
from cdp_browser_agent.mcp_server import create_mcp_server
from cdp_browser_agent.web import network, tools as webtools
from cdp_browser_agent.web.extract import extract_page, parse_search
from cdp_browser_agent.web.network import WebError
from cdp_browser_agent.web.tools import WebTools


@pytest.fixture(autouse=True)
def reset_route_cache():
    network._SUCCESSFUL_ROUTES.clear()
    yield
    network._SUCCESSFUL_ROUTES.clear()


@pytest.fixture
def site(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            content_type = "text/html; charset=utf-8"
            status, headers = 200, {}
            if self.path == "/dynamic":
                text = '<html><title>Dynamic fixture</title><body><script>document.body.innerHTML="<h1>Rendered by JavaScript: verified browser fallback.</h1>"</script></body></html>'
            elif self.path == "/private-redirect":
                status, headers, text = 302, {"Location": "http://169.254.169.254/metadata"}, ""
            elif self.path == "/cross-origin":
                status, headers, text = 302, {"Location": "https://example.com/"}, ""
            elif self.path == "/login":
                text = '<title>Sign in</title><input type="password"><p>Sign in to continue.</p>'
            elif self.path == "/large":
                text = "x" * 5000
            elif self.path == "/429":
                status, text = 429, "Slow down"
            elif self.path == "/json":
                content_type, text = "application/json", '{"value":42}'
            elif self.path == "/feed":
                content_type, text = "Application/Rss+Xml; charset=utf-8", '<rss><channel><item><title>One</title><description>Full source body</description></item></channel></rss>'
            elif self.path == "/vendor-json":
                content_type, text = "application/vnd.example+json", '{"records":[{"title":"One","body":"Full source body"}]}'
            elif self.path.startswith("/gzip"):
                content_type, text = "text/plain", "Source text. " * (1000 if self.path == "/gzip-large" else 5)
                headers["Content-Encoding"] = "gzip"
            else:
                text = '<html><title>Fixture</title><body><nav>Unrelated menu</nav><article><h1>Verified article</h1><p>' + "Actual source text. " * 500 + '</p><script>secret-script</script><a href="/json">JSON source</a></article></body></html>'
            body = text.encode()
            if self.path.startswith("/gzip"):
                body = gzip.compress(body)
                if self.path == "/gzip-broken":
                    body = body[:-8]
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    settings = {"proxy": "", "allowed_private_hosts": ["127.0.0.1"], "artifact_dir": str(tmp_path / "web")}
    try:
        yield f"http://127.0.0.1:{server.server_port}", settings
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


async def test_fetch_extract_paging_and_source_evidence(site):
    url, settings = site
    web = WebTools(settings)
    first = await web.fetch(url, max_chars=500)
    assert first["ok"] and first["truncated"] and first["next_offset"] == 500
    assert first["network_route"] == "direct"
    second = await web.fetch(url, offset=500, max_chars=500)
    assert second["cache_hit"] and second["offset"] == 500
    text_path, raw_path, receipt = map(Path, first["artifact_paths"])
    text = text_path.read_text(encoding="utf-8")
    assert "Actual source text" in text and "secret-script" not in text and "Unrelated menu" not in text
    assert first["text"] + second["text"] == text[:1000]
    metadata = json.loads(receipt.read_text(encoding="utf-8"))
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == metadata["response_sha256"]
    assert hashlib.sha256(text_path.read_bytes()).hexdigest() == metadata["text_sha256"]
    assert first["links"][0]["url"] == url + "/json"


async def test_fetch_classifies_blocked_dynamic_and_limits(site):
    url, settings = site
    web = WebTools(settings)
    assert (await web.fetch(url + "/dynamic"))["status"] == "insufficient_static_content"
    assert (await web.fetch(url + "/login"))["status"] == "authentication_required"
    limited = await WebTools({**settings, "max_response_bytes": 1024}).fetch(url + "/large")
    assert limited["status"] == "too_large" and limited["needs_browser"]
    rate = await web.fetch(url + "/429")
    assert rate["status"] == "rate_limited" and not rate["needs_browser"]
    assert (await web.fetch(url + "/json"))["text"] == '{"value":42}'


async def test_unsolicited_compression_is_bounded_and_preserves_wire_evidence(site):
    url, settings = site
    web = WebTools({**settings, "max_response_bytes": 1024})
    result = await web.fetch(url + "/gzip")
    assert result["ok"] and result["content_encoding"] == "gzip"
    _, decoded, receipt, encoded = map(Path, result["artifact_paths"])
    assert gzip.decompress(encoded.read_bytes()) == decoded.read_bytes()
    metadata = json.loads(receipt.read_text(encoding="utf-8"))
    assert hashlib.sha256(encoded.read_bytes()).hexdigest() == metadata["encoded_sha256"]
    assert hashlib.sha256(decoded.read_bytes()).hexdigest() == metadata["response_sha256"]
    assert (await web.fetch(url + "/gzip-large"))["status"] == "too_large"
    assert (await web.fetch(url + "/gzip-broken"))["status"] == "invalid_encoding"


@pytest.mark.parametrize('path,kind', [('/feed', 'xml'), ('/vendor-json', 'json')])
async def test_structured_media_suffix_reaches_document_pipeline_with_full_source(site, tmp_path, path, kind):
    from cdp_browser_agent.documents.engine import DocumentTools, digest
    url, settings = site
    web = WebTools(settings)
    service = DocumentTools({'documents': {'state_dir': str(tmp_path / 'documents')}}, web)
    result = await service.open(url + path)
    assert result['ok'] and result['format'] == kind
    raw, meta = service._source(result['source_id'])
    assert digest(raw) == meta['sha256'] and meta['requested_url'] == url + path
    if kind == 'xml':
        candidate = await service.preview(result['source_id'], {'mode': 'elements', 'selector': './/item'})
        assert service._job(candidate['job_id'])[2]['records'][0]['text'] == 'OneFull source body'
    else:
        inspected = await service.inspect(result['source_id'], '/records/0/body')
        assert inspected['value'] == 'Full source body'


def test_compression_formats_and_trailing_data_fail_closed():
    import zlib
    assert network.decode_response(zlib.compress(b"exact source"), "deflate", 1024) == b"exact source"
    for body, encoding in [(b"bad gzip", "gzip"), (gzip.compress(b"first") + gzip.compress(b"second"), "gzip"), (b"br", "br")]:
        with pytest.raises(WebError):
            network.decode_response(body, encoding, 1024)


async def test_redirect_and_private_targets_are_rejected(site):
    url, settings = site
    assert (await WebTools({"proxy": ""}).fetch(url))["status"] == "restricted_url"
    assert (await WebTools(settings).fetch(url + "/private-redirect"))["status"] == "restricted_url"
    web = WebTools(settings)
    web.allowed_origins = [url]
    assert (await web.fetch(url + "/cross-origin"))["status"] == "restricted_url"
    result = await web.fetch("https://name:secret@example.com")
    assert result["status"] == "invalid_url" and "secret" not in json.dumps(result)


async def test_mixed_public_private_dns_is_not_accepted(monkeypatch):
    resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                                      (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    with pytest.raises(WebError, match="Private"):
        await network.resolve_public(httpx.URL("https://test.example"))


def test_search_parsers_distinguish_challenge_from_empty():
    html = b'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">Example</a><a class="result__snippet">Source excerpt</a></div>'
    rows = parse_search("duckduckgo", html)
    assert rows == [{"title": "Example", "url": "https://example.com", "snippet": "Source excerpt"}]
    assert parse_search("duckduckgo", b'<div class="no-results">No results</div>') == []
    for body, status in [(b'<script src="anomaly.js"></script>', "challenge"), (b'<html>Unknown shell</html>', "parse_error")]:
        with pytest.raises(WebError) as caught:
            parse_search("duckduckgo", body)
        assert caught.value.status == status
    rss = b'<rss><channel><item><title>One</title><link>https://example.com</link><description>Evidence</description></item></channel></rss>'
    assert parse_search("bing_rss", rss)[0]["url"] == "https://example.com"


def test_web_action_aliases_normalize_into_registered_tools():
    assert validate_action({"action": "web_fetch", "url": "https://example.com"}, {}) == {
        "action": "tool", "name": "web_fetch", "arguments": {"url": "https://example.com"}}
    assert validate_action({"action": "web_search", "query": "query"}, {})["name"] == "web_search"
    with pytest.raises(ValueError):
        validate_action({"action": "invented_tool"}, {})


def test_proxy_auto_discovers_and_invalidates_cached_route(monkeypatch):
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    routes = network.network_routes("auto")
    assert routes[0] == ("environment_proxy", "http://proxy.example:8080")
    assert routes.count(routes[0]) == 1 and routes[-1] == ("direct", None)
    network.remember_route("auto", "fetch", ("direct", None))
    assert network.ordered_routes("auto", "fetch")[0] == ("direct", None)
    monkeypatch.setenv("HTTPS_PROXY", "http://changed.example:8080")
    assert network.ordered_routes("auto", "fetch")[0][0] == "environment_proxy"
    assert network.network_routes("") == [("direct", None)]


async def test_no_proxy_bypasses_auto_proxy(monkeypatch):
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "example.com")
    request = AsyncMock(return_value={"status_code": 200, "url": "https://example.com",
                                     "body": b"", "headers": {}, "redirects": []})
    monkeypatch.setattr(webtools, "download", request)
    await WebTools()._request("https://example.com", purpose="fetch", attempts=[])
    assert request.call_args.kwargs["proxy"] is None


def test_extraction_handles_nested_hidden_elements():
    body = b'<html><nav><header>Menu</header></nav><article><div style="display:none"><p style="color:red">Hidden</p></div><p>Visible text</p></article></html>'
    page = extract_page(body, "text/html", "https://example.com")
    assert page["text"] == "Visible text"


async def test_search_provider_errors_are_not_empty_success(monkeypatch):
    response = {"status_code": 200, "url": "https://html.duckduckgo.com/html/", "headers": {},
                "body": b'<script src="anomaly.js"></script>', "redirects": []}
    monkeypatch.setattr(webtools, "download", AsyncMock(return_value=response))
    result = await WebTools({"search": {"provider": "duckduckgo"}, "proxy": ""}).search("query")
    assert not result["ok"] and result["status"] == "challenge" and result["needs_browser"]
    response["body"] = b'<rss><channel><item><title>Result</title><link>https://example.com</link><description>Text</description></item></channel></rss>'
    good = await WebTools({"proxy": ""}).search("query")
    assert good["ok"] and good["status"] == "success"


async def test_both_tools_failover_without_proxy_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "network_routes", lambda _: [("environment_proxy", "http://user:private-token@proxy.invalid:8080"), ("direct", None)])
    calls = []
    async def request(url, **kwargs):
        calls.append(kwargs["proxy"])
        if kwargs["proxy"]:
            raise httpx.ConnectError("Credential-bearing exception must not be returned")
        rss = b'<rss><channel><item><title>Fixture</title><link>https://example.com</link><description>Evidence</description></item></channel></rss>'
        body = b'<html><title>Page</title><article>' + b'Actual content. ' * 20 + b'</article></html>'
        return {"body": rss if "bing.com/search" in url else body, "url": url,
                "status_code": 200, "headers": {"content-type": "text/html"}, "redirects": []}
    monkeypatch.setattr(webtools, "download", request)
    web = WebTools({"artifact_dir": str(tmp_path)})
    for result in [await web.search("query"), await web.fetch("https://example.com")]:
        assert result["ok"] and result["network_route"] == "direct"
        assert [a["route"] for a in result["network_attempts"]] == ["environment_proxy", "direct"]
        assert "private-token" not in json.dumps(result) and "Credential-bearing" not in json.dumps(result)
    before = len(calls)
    await web.fetch("https://another.example")
    assert calls[before:] == [None]  # Successful route reused for subsequent reads.


async def test_proxy_http_challenge_does_not_rotate_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "network_routes", lambda _: [("environment_proxy", "http://proxy.invalid:8080"), ("direct", None)])
    request = AsyncMock(return_value={"status_code": 403, "url": "https://example.com", "headers": {}, "body": b"", "redirects": []})
    monkeypatch.setattr(webtools, "download", request)
    result = await WebTools({"artifact_dir": str(tmp_path)}).fetch("https://example.com")
    assert not result["ok"] and result["needs_browser"]
    assert request.await_count == 1


async def test_web_mcp_tools_do_not_call_model_or_browser(site, monkeypatch):
    url, settings = site
    launch = AsyncMock(side_effect=AssertionError("Must not launch browser"))
    planner = AsyncMock(side_effect=AssertionError("Must not call model"))
    monkeypatch.setattr(agent.BrowserController, "launch", launch)
    monkeypatch.setattr(agent, "plan_next_action", planner)
    config = browser_agent_default_config()
    config["web"].update(settings)
    async with Client(create_mcp_server(config)) as client:
        result = await client.call_tool("web_fetch", {"url": url, "max_chars": 500})
        assert not result.is_error and result.structured_content["title"] == "Fixture"
    launch.assert_not_awaited()
    planner.assert_not_awaited()


@pytest.mark.parametrize("dynamic", [False, True])
async def test_fast_path_then_real_browser_only_when_needed(site, tmp_path, monkeypatch, dynamic):
    url, settings = site
    target = url + ("/dynamic" if dynamic else "/")
    config = browser_agent_default_config()
    config["web"].update(settings)
    config["browser"].update(headless=True, focus_page=False, downloads_path=str(tmp_path / "browser"))
    config["agent"].update(max_steps=4, log_dir=str(tmp_path / "logs"))
    real_launch = agent.BrowserController.launch
    launch = AsyncMock(wraps=real_launch)
    monkeypatch.setattr(agent.BrowserController, "launch", launch)
    async def planner(request):
        if request["step"] == 1:
            assert not request["browser_started"]
            return {"action": {"action": "tool", "name": "web_fetch", "arguments": {"url": target}}}
        if dynamic and request["step"] == 2:
            assert request["last_result"]["needs_browser"]
            return {"action": {"action": "navigate", "url": request["last_result"]["browser_url"]}}
        if dynamic:
            assert "Rendered by JavaScript" in request["observation"]["fullText"]
        else:
            assert request["last_result"]["ok"] and request["sources"][-1]["kind"] == "web_fetch"
        return {"action": {"action": "done", "outcome": "completed", "answer": "Source content verified"}}
    monkeypatch.setattr(agent, "plan_next_action", planner)
    result = await run_browser_agent("Read the source page", config)
    assert result["status"] == "completed" and result["browser_started"] == dynamic
    assert launch.await_count == int(dynamic)
    assert result["collected_files"]


async def test_disabled_web_tools_and_deadline(tmp_path, monkeypatch):
    disabled = WebTools({"enabled": False})
    assert (await disabled.fetch("https://example.com"))["status"] == "disabled"
    assert (await disabled.search("query"))["status"] == "disabled"
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(webtools, "download", slow)
    result = await WebTools({"proxy": "", "timeout_seconds": 1}).search("query")
    assert result["status"] == "timeout" and result["needs_browser"]
