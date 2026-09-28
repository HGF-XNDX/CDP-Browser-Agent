from __future__ import annotations

from copy import deepcopy
from fnmatch import fnmatchcase
import hashlib
import json
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator
import soupsieve

from ..web.extract import decode
from ..web.network import valid_url


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 8000}
SELECTOR = {"type": "string", "minLength": 1, "maxLength": 500}
PATTERNS = {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 500}}
FIELDS = {"type": "object", "minProperties": 1, "maxProperties": 32,
    "propertyNames": {"pattern": "^[a-zA-Z][a-zA-Z0-9_]{0,63}$"},
    "additionalProperties": obj({"selector": {**SELECTOR, "description": "CSS descendant selector relative to the item. Omit selector or use :scope to read the item itself, e.g. {attribute: 'data-id'} for its own data-id."}, "attribute": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"},
        "required": {"type": "boolean"}, "url": {"type": "boolean"}})}
CRAWL_SCHEMA = obj({
    "seed_urls": {"type": "array", "minItems": 1, "maxItems": 50, "items": TEXT},
    "max_pages": {"type": "integer", "minimum": 1, "maximum": 500},
    "max_depth": {"type": "integer", "minimum": 0, "maximum": 10},
    "max_records": {"type": "integer", "minimum": 1, "maximum": 10000},
    "link_selector": SELECTOR, "next_selector": SELECTOR,
    "include_patterns": PATTERNS, "exclude_patterns": PATTERNS, "record_patterns": PATTERNS,
    "item_selector": {**SELECTOR, "description": "CSS matching record containers. Field selectors search descendants; root attributes use no field selector."}, "fields": FIELDS, "allow_empty": {"type": "boolean"},
    "key_fields": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True, "items": TEXT}
}, ["seed_urls"])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def canonical(url):
    return str(valid_url(urldefrag(url)[0]))


def origin(url):
    value = urlsplit(url)
    port = value.port or (443 if value.scheme == "https" else 80)
    return f"{value.scheme}://{value.hostname}:{port}"


def matches(url, patterns):
    return not patterns or any(fnmatchcase(url, pattern) for pattern in patterns)


def in_scope(url, spec):
    return (origin(url) in {origin(u) for u in spec["seed_urls"]}
            and matches(url, spec.get("include_patterns"))
            and not any(fnmatchcase(url, p) for p in spec.get("exclude_patterns", [])))


def prepare(spec, settings):
    Draft202012Validator(CRAWL_SCHEMA).validate(spec)
    spec = {"max_pages": min(20, settings["max_pages"]), "max_depth": 1,
            "max_records": min(1000, settings["max_records"]), "link_selector": "a[href]", **deepcopy(spec)}
    spec["seed_urls"] = list(dict.fromkeys(canonical(url) for url in spec["seed_urls"]))
    for key in ("max_pages", "max_records"):
        if spec[key] > settings[key]:
            raise ValueError(f"Crawl {key} exceeds the operator limit")
    if not all(in_scope(url, spec) for url in spec["seed_urls"]):
        raise ValueError("A crawl seed is excluded by its own URL filters")
    for key in ("link_selector", "next_selector", "item_selector"):
        if spec.get(key):
            soupsieve.compile(spec[key])
    for field in spec.get("fields", {}).values():
        if field.get("selector"):
            soupsieve.compile(field["selector"])
    if spec.get("item_selector") and not spec.get("fields"):
        raise ValueError("item_selector requires fields")
    if not set(spec.get("key_fields", [])) <= set(spec.get("fields", {"title": {}, "text": {}})):
        raise ValueError("key_fields must name extracted fields")
    return spec


def parse_page(raw, content_type, url, text, title, spec, depth):
    soup = BeautifulSoup(decode(raw, content_type), "html.parser")
    base = soup.find("base", href=True)
    base_url = canonical(urljoin(url, base["href"])) if base else url
    for node in soup.select("script,style,noscript,template"):
        node.decompose()
    links = []
    for selector, level in ((spec["link_selector"], depth+1), (spec.get("next_selector"), depth)):
        if not selector or level > spec["max_depth"]:
            continue
        nodes = soup.select(selector, limit=1001)
        if len(nodes) > 1000:
            raise ValueError("Link selector matches more than 1000 nodes; narrow the selector")
        for node in nodes:
            if not node.get("href"):
                continue
            try:
                target = canonical(urljoin(base_url, node["href"]))
                if in_scope(target, spec):
                    links.append({"url": target, "depth": level})
            except ValueError:
                continue
    rows = []
    if matches(url, spec.get("record_patterns")):
        if not spec.get("fields"):
            rows = [{"title": title, "text": text}]
        else:
            roots = soup.select(spec["item_selector"], limit=1001) if spec.get("item_selector") else [soup]
            if len(roots) > 1000:
                raise ValueError("More than 1000 items on one page; narrow the selector")
            for root in roots:
                row = {}
                for name, field in spec["fields"].items():
                    node = root.select_one(field["selector"]) if field.get("selector") not in (None, ":scope") else root
                    value = (node.get(field["attribute"]) if field.get("attribute") else node.get_text(" ", strip=True)) if node is not None else None
                    if value is not None:
                        value = " ".join(value) if isinstance(value, list) else str(value).strip()
                    if not value and field.get("required", True):
                        raise ValueError(f"Required crawl field missing: {name}; selector={field.get('selector')!r}. Selectors search descendants; for the item's own attribute omit selector or use :scope. Inspect web_fetch(content_format='html') before revising the spec.")
                    if value and field.get("url"):
                        value = canonical(urljoin(base_url, value))
                    row[name] = value
                rows.append(row)
        if not rows and not spec.get("allow_empty", False):
            raise ValueError("No matching items; use allow_empty only when an empty result is expected")
        if any(len(json.dumps(row, ensure_ascii=False)) > 200000 for row in rows):
            raise ValueError("Crawl record exceeds 200000 characters; select narrower fields")
    return rows, links
