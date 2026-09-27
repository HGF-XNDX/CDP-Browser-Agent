"""Deterministic local collection fixture used by unit and opt-in live tests."""
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests[self.path] += 1
        parts = urlsplit(self.path)
        if self.path in self.server.fail_paths:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Temporarily unavailable")
            return
        if parts.path == "/start":
            html = '<h1>Supplier portal</h1><a href="/catalog?page=1">Open catalog</a>'
        elif parts.path == "/catalog":
            page = parse_qs(parts.query).get("page", ["1"])[0]
            skus = ["A17", "B28"] if page == "1" else ["A17", "C39"]
            html = '<title>Product catalog</title><h1>Product catalog</h1><div id="catalog">'
            for sku in skus:
                title, price = self.server.products[sku]
                html += f'<article class="product" data-sku="{sku}"><a href="/product/{sku}">{title}</a><span class="price">{price}</span></article>'
            html += '</div>'
            if page == "1":
                html += '<a class="next" href="/catalog?page=2">Next</a>'
        elif parts.path.startswith("/product/"):
            sku = parts.path.rsplit("/", 1)[-1]
            html = f'<h1>{sku}</h1><p class="description">Verified details for {sku}.</p>'
        elif parts.path == "/empty":
            html = '<h1>No matching products</h1><p class="empty">No products</p>'
        else:
            html = '<h1>Unknown page</h1>'
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def demo_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.products = {"A17": ("Desk lamp", "129"), "B28": ("Desk lamp Pro", "179"), "C39": ("Budget lamp", "89")}
    server.requests = Counter()
    server.fail_paths = set()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        with demo_server() as (_, url):
            print(url, flush=True)
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
