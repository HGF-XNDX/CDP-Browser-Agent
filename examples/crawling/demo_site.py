"""Local deterministic collection fixture; shared by tests and opt-in model evaluation."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import urlsplit, parse_qs


@contextmanager
def demo_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.server.requests.append((self.path, time.time()))
            path = urlsplit(self.path).path
            status, headers = 200, {"Content-Type": "text/html; charset=utf-8"}
            if path == "/robots.txt":
                body = self.server.robots
                headers["Content-Type"] = "text/plain"
            elif path == "/rate":
                status, body = 429, "Slow down"
                headers["Retry-After"] = "1"
            elif path == "/unavailable":
                status, body = 503, "Unavailable"
            elif path == "/redirect":
                status, body = 302, ""
                headers["Location"] = self.server.redirect
            elif path == "/dynamic":
                body = '<html><body><script>document.body.innerHTML="Rendered result"</script></body></html>'
            elif path == "/login":
                body = '<html><body><input type="password"><p>Please sign in</p></body></html>'
            else:
                if path == "/slow":
                    time.sleep(.8)
                if path == "/list":
                    second = parse_qs(urlsplit(self.path).query).get("page") == ["2"]
                    rows = [("b", "Beta"), ("c", "Gamma")] if second else [("a", "Alpha"), ("b", "Beta")]
                    links = '' if second else '<a class="next" href="/list?page=2">Next</a>'
                elif path == "/many":
                    rows, links = [(str(i), "Item " + str(i)) for i in range(25)], ''
                elif path == "/conflict":
                    rows, links = [("a", "Alpha"), ("a", "Different")], ''
                elif path == "/index":
                    rows = [("index", "Index")]
                    links = '<a href="/detail/a">A</a><a href="/detail/a#fragment">A again</a><a href="/detail/b">B</a><a href="/excluded">Excluded</a><a href="https://example.org/out">Outside</a>'
                else:
                    rows, links = [(path, "Example document")], ''
                items = ''.join(f'<article class="item" data-id="{key}"><h2>{title}</h2></article>' for key, title in rows)
                body = '<html><title>Collection fixture</title><body><main><h1>Public documents</h1>' + items + links + '<p>This is a controlled static collection fixture with explicit source records and ordinary pagination links.</p></main></body></html>'
            data = body.encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.robots = 'User-agent: *\nDisallow: /blocked*\n'
    server.redirect = "/blocked/secret"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
