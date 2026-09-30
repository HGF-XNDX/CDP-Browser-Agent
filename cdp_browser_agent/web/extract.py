from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .network import WebError, valid_url


def clean_text(value):
    return "\n".join(line for line in (re.sub(r"[\t \u00a0]+", " ", line).strip()
                                      for line in value.replace("\r", "").splitlines()) if line)


def decode(body, content_type):
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    if not charset:
        charset = re.search(rb"charset\s*=\s*[\"']?([\w-]+)", body[:4096], re.I)
    encoding = charset.group(1) if charset else "utf-8"
    if isinstance(encoding, bytes):
        encoding = encoding.decode("ascii", errors="replace")
    try:
        return body.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        # BeautifulSoup's detector handles older Chinese sites without changing
        # the retained original response bytes.
        return str(BeautifulSoup(body, "html.parser"))


def extract_page(body, content_type, url):
    text = decode(body, content_type)
    is_html = "html" in content_type or bool(re.match(r"\s*(?:<!doctype html|<html)", text, re.I))
    if not is_html:
        media_type = content_type.partition(';')[0].strip().lower()
        if media_type.startswith("text/") or media_type in {"application/json", "application/xml"} or media_type.endswith(('+xml', '+json')):
            return {"title": "", "text": text.strip(), "links": [], "extraction": "plain_text", "needs_browser": False}
        raise WebError("unsupported_content", "web_fetch supports HTML, text, XML and JSON (including +xml/+json media types); use an appropriate download/import capability for other resources", needs_browser=True)
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    head = (title + " " + soup.get_text(" ", strip=True)[:800]).lower()
    challenge = bool(soup.select_one('form#challenge-form, form#challengeForm, #cf-challenge-running, .anomaly-modal'))
    challenge |= any(x in title.lower() for x in ("just a moment", "security verification", "verify you are human", "人机验证", "访问验证"))
    if challenge:
        raise WebError("challenge", "Page requires access verification; browser/human interaction may be necessary", needs_browser=True)
    login = bool(soup.select_one('input[type="password"]'))
    has_scripts = soup.find("script") is not None
    for tag in soup.select('script, style, noscript, template, svg, nav, header, footer, aside, [hidden], [aria-hidden="true"]'):
        if not tag.decomposed:
            tag.decompose()
    for tag in list(soup.select('[style]')):
        if not tag.decomposed and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", tag.get("style", ""), re.I):
            tag.decompose()
    candidates = soup.select('article, main, [role="main"]')
    root = max(candidates, key=lambda x: len(x.get_text()), default=soup.body or soup)
    content = clean_text(root.get_text("\n", strip=True))
    links, seen = [], set()
    for anchor in root.select("a[href]"):
        try:
            target = str(valid_url(urljoin(url, anchor.get("href", ""))))
        except WebError:
            continue
        if target not in seen:
            links.append({"url": target[:2000], "text": anchor.get_text(" ", strip=True)[:160]})
            seen.add(target)
        if len(links) >= 12:
            break
    dynamic = not content or (has_scripts and len(content) < 80) or (len(content) < 600 and any(x in head for x in (
        "enable javascript", "javascript is required", "请启用javascript", "正在加载")))
    return {"title": title[:500], "text": content, "links": links,
            "extraction": "main_content" if candidates else "body_text",
            "needs_browser": login or dynamic, "fallback_reason": "authentication_required" if login else ("insufficient_static_content" if dynamic else None)}


def parse_search(provider, body):
    text = body.decode("utf-8", errors="replace")
    if provider == "bing_rss":
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise WebError("parse_error", "Unexpected XML declarations")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise WebError("parse_error", "Search provider did not return RSS", needs_browser=True) from None
        if root.tag != "rss" or root.find("channel") is None:
            raise WebError("parse_error", "Search provider did not return a search feed", needs_browser=True)
        return [{"title": x.findtext("title") or "", "url": x.findtext("link") or "",
                 "snippet": clean_text(BeautifulSoup(x.findtext("description") or "", "html.parser").get_text(" "))}
                for x in root.findall("./channel/item")]
    soup = BeautifulSoup(text, "html.parser")
    if "anomaly.js" in text or soup.select_one(".anomaly-modal, #challenge-form"):
        raise WebError("challenge", "Search provider returned a verification page", needs_browser=True)
    results = []
    for anchor in soup.select("a.result__a"):
        target = urljoin("https://html.duckduckgo.com/", anchor.get("href", ""))
        parsed = urlsplit(target)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg", [target])[0]
        container = anchor.find_parent(class_="result")
        snippet = container.select_one(".result__snippet") if container else None
        results.append({"title": anchor.get_text(" ", strip=True), "url": target,
                        "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
    if not results and not soup.select_one(".no-results, .result--no-result"):
        raise WebError("parse_error", "No recognized search results or explicit empty-result marker", needs_browser=True)
    return results
