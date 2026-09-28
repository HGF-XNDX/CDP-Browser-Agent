from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx


class WebError(ValueError):
    def __init__(self, status, message, *, needs_browser=False):
        super().__init__(message)
        self.status, self.needs_browser = status, needs_browser


def valid_url(value):
    if not isinstance(value, str) or len(value) > 8000 or any(ord(c) < 32 for c in value):
        raise WebError("invalid_url", "Expected a bounded HTTP(S) URL")
    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            raise ValueError()
        if "%" in url.host or "\\" in value:
            raise ValueError()
        return url.copy_with(fragment=None)
    except (ValueError, httpx.InvalidURL):
        raise WebError("invalid_url", "Only HTTP(S) URLs without embedded credentials are supported") from None


def origin(value):
    url = valid_url(value)
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


async def resolve_public(url, allowed_private_hosts=()):
    host = url.host
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, url.port or (443 if url.scheme == "https" else 80), type=socket.SOCK_STREAM), 5)
        addresses = list(dict.fromkeys(item[4][0] for item in infos))
    if not addresses:
        raise WebError("network_error", "DNS returned no address", needs_browser=True)
    if host not in allowed_private_hosts:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global):
                raise WebError("restricted_url", "Private, loopback, link-local and reserved targets are not enabled")
    # Prefer IPv4 where both families exist. Connect to this validated address, not
    # a second hostname resolution. TLS still verifies the original hostname.
    return sorted(addresses, key=lambda x: ":" in x)[0]


_SUCCESSFUL_ROUTES = {}


def network_routes(mode):
    """Use configured routes; never install proxies or expose their credentials."""
    if mode != "auto":
        return [("configured_proxy", mode)] if mode else [("direct", None)]
    routes = []
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(key):
            routes.append(("environment_proxy", os.environ[key]))
    if os.name == "nt":
        from urllib.request import getproxies_registry
        proxies = getproxies_registry()
        if proxies.get("https") or proxies.get("http"):
            routes.append(("system_proxy", proxies.get("https") or proxies["http"]))
    routes.append(("direct", None))
    seen = set()
    return [(kind, proxy) for kind, proxy in routes if not (proxy in seen or seen.add(proxy))]


def ordered_routes(mode, purpose):
    routes = network_routes(mode)
    key = (purpose, tuple(routes))
    preferred = _SUCCESSFUL_ROUTES.get(key)
    if preferred and preferred[1] > time.monotonic():
        routes.sort(key=lambda route: route != preferred[0])
    return routes


def remember_route(mode, purpose, route):
    # A changed proxy configuration gets a different key. Retain a bounded cache.
    if len(_SUCCESSFUL_ROUTES) > 32:
        _SUCCESSFUL_ROUTES.clear()
    _SUCCESSFUL_ROUTES[(purpose, tuple(network_routes(mode)))] = (route, time.monotonic() + 300)


async def download(url, *, timeout=12, max_bytes=2_000_000, public_only=True,
                   allowed_private_hosts=(), allowed_origins=None, proxy=None,
                   method="GET", json_body=None, headers=None, before_request=None):
    current = valid_url(url)
    original_origin = origin(str(current))
    redirects = []
    # One client per request chain prevents pooled connections for different TLS
    # hostnames sharing a pinned IP, and does not import browser/session cookies.
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 6)), trust_env=False,
                                 limits=httpx.Limits(max_keepalive_connections=0),
                                 proxy=proxy, follow_redirects=False,
                                 headers={"User-Agent": "CDP-Browser-Agent/0.5 (+public-web-reader)",
                                          "Accept-Encoding": "identity"}) as client:
        for _ in range(6):
            if allowed_origins is not None and origin(str(current)) not in {origin(x) for x in allowed_origins}:
                raise WebError("restricted_url", "URL is outside the workflow origins")
            if before_request:
                await before_request(str(current))
            pinned = current
            extra = {}
            request_headers = dict(headers or {})
            if public_only:
                address = await resolve_public(current, allowed_private_hosts)
                if proxy is None:
                    pinned = current.copy_with(host=address)
                    request_headers["Host"] = current.netloc.decode("ascii")
                    extra["sni_hostname"] = current.host
                # HTTP CONNECT in httpcore uses the target hostname for TLS and
                # does not support an independent SNI extension. Preserve the
                # hostname through a trusted operator proxy (never disable TLS
                # verification). Such a proxy controls its own DNS/egress policy.
            # Search API credentials never follow a redirect to a different host.
            if headers and origin(str(current)) != original_origin:
                raise WebError("redirect_rejected", "Authenticated search endpoint redirected to another origin")
            client.cookies.clear()
            async with client.stream(method, pinned, headers=request_headers, json=json_body,
                                     extensions=extra) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise WebError("redirect_error", "Redirect has no Location", needs_browser=True)
                    redirects.append(str(current))
                    current = valid_url(urljoin(str(current), location))
                    continue
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise WebError("too_large", "Response exceeds the configured byte limit", needs_browser=True)
                # Reject unsolicited compression rather than inflating an unbounded
                # compression bomb in the HTTP decoder. We request identity above.
                if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise WebError("unsupported_encoding", "Server ignored identity encoding; use browser", needs_browser=True)
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise WebError("too_large", "Response exceeds the configured byte limit", needs_browser=True)
                return {"url": str(current), "status_code": response.status_code,
                        "headers": dict(response.headers), "body": bytes(body), "redirects": redirects}
    raise WebError("redirect_limit", "Too many redirects", needs_browser=True)
