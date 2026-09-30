from __future__ import annotations

import asyncio
import base64
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag
from jsonschema import Draft202012Validator
import regex

from ..harness.tools import Tool
from ..web.extract import decode
from ..web.tools import WebTools
from ..workflows.store import atomic_json
from .labels import label_values


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(value).hexdigest()


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


SELECTOR = {"type": "string", "minLength": 1, "maxLength": 500}
PATTERN = {"type": "string", "minLength": 1, "maxLength": 300}
SPEC_SCHEMA = schema({
    "mode": {"enum": ["elements", "sections"]},
    "selector": {**SELECTOR, "description": "elements: complete record nodes. sections: union of disjoint heading AND content units, e.g. 'h2.topic, p'; NEVER a body/div container enclosing those units."},
    "heading_selector": {**SELECTOR, "description": "sections only: headings that start groups; every match must also be in selector. Use observed tags/classes, not guesses."},
    "start_pattern": {**PATTERN, "description": "Starts a NEW record. Match only the requested record unit. Do not add non-record structural boundaries here merely to preserve them; use exclude_pattern for those."},
    "exclude_pattern": {**PATTERN, "description": "Ends the previous record WITHOUT creating another record. The matched structural node remains intact in remainder; it is NOT deleted. Use when an inter-record heading must stay outside record bodies."},
    "body_selector": {**SELECTOR, "description": "sections: body units (subset of selector), others become annotations. elements: relative descendants holding body paragraphs. Omit to retain all selected text."},
    "title_selector": SELECTOR,
    "key_pattern": {**PATTERN, "description": "Capture group 1 extracts a label from each heading. When supplied, EVERY heading must match; unmatched headings block export. Test on actual text and check regex escaping."},
    "key_separator": {**PATTERN, "description": "Splits the label extracted by key_pattern, not the original heading. Capture the entire grouped label first; content outside that capture cannot be split."},
    "key_source": {**schema({"selector": SELECTOR, "attribute": {"type": "string", "minLength": 1, "maxLength": 120}}),
        "description": "Read a label from one relative node, independently of body/title. selector defaults to '.' (the record start node); attribute reads its actual attribute, otherwise its text."},
    "key_transforms": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"oneOf": [
        schema({"operation": {"const": "capture"}, "pattern": PATTERN}, ["operation", "pattern"]),
        schema({"operation": {"const": "split"}, "pattern": PATTERN}, ["operation", "pattern"]),
        schema({"operation": {"const": "integer_range"}, "delimiter": {"type": "string", "minLength": 1, "maxLength": 8},
            "max_values": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["operation", "delimiter"])]},
        "description": "Ordered label-only pipeline: capture group 1, split by regex (delimiter captures are not labels), expand two integer endpoints using a literal delimiter. Non-range labels remain unchanged. Use observed source values; never combine with legacy key_pattern/key_separator. Each step is saved with input/output; expansion is bounded."},
    "collection_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"},
    "metadata": {"type": "object", "maxProperties": 20},
}, ["mode", "selector"])


@dataclass
class Node:
    path: str
    tag: str
    attrs: dict
    content: list = field(default_factory=list)

    def text(self):
        return "".join(x.text() if isinstance(x, Node) else x if isinstance(x, str) else "" for x in self.content)

    def tree(self, replacements=None):
        if replacements and self.path in replacements:
            return {"fragment_ref": self.path}
        return {"path": self.path, "tag": self.tag, "attrs": self.attrs,
                "content": [x.tree(replacements) if isinstance(x, Node) else x for x in self.content]}


class DocumentTree:
    """One canonical ordered tree; selectors never replace the preserved source."""

    def __init__(self, raw, kind, content_type="", max_nodes=100000):
        self.kind, self.nodes, self.native, self.by_native = kind, {}, {}, {}
        self.max_nodes = max_nodes
        if kind == "xml":
            # Prohibit DTD/entity declarations before parsing, including UTF-16/32 input.
            if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", raw.replace(b"\x00", b""), re.I):
                raise ValueError("XML DTD/entity declarations are not supported")
            self.document = ET.fromstring(raw)
        else:
            self.document = BeautifulSoup(decode(raw, content_type), "html.parser")
        self.root = self._build(self.document, "/", 0)

    def _build(self, native, path, depth):
        if depth > 100 or len(self.nodes) >= self.max_nodes:
            raise ValueError("Document exceeds tree depth/node budget")
        xml = self.kind == "xml"
        tag = str(native.tag) if xml else native.name
        node = Node(path, tag, dict(native.attrib if xml else native.attrs))
        self.nodes[path], self.native[path], self.by_native[id(native)] = node, native, node
        counts = Counter()
        if xml and native.text:
            node.content.append(native.text)
        for child in list(native) if xml else native.contents:
            if xml or isinstance(child, Tag):
                name = str(child.tag) if xml else child.name
                counts[name] += 1
                child_path = path.rstrip("/") + f"/{name}[{counts[name]}]"
                node.content.append(self._build(child, child_path, depth + 1))
                if xml and child.tail:
                    node.content.append(child.tail)
            elif isinstance(child, (Comment, Doctype)):
                node.content.append({"comment" if isinstance(child, Comment) else "doctype": str(child)})
            elif isinstance(child, NavigableString):
                node.content.append(str(child))
        return node

    def select(self, selector, node=None):
        if not isinstance(selector, str) or not 1 <= len(selector) <= 500:
            raise ValueError("Selector length must be 1..500")
        native = self.native[node.path] if node else self.document
        if selector == ".":
            return [node or self.root]
        if self.kind == "xml" and selector.startswith("/"):
            # Accept conventional document-root/descendant paths without changing case.
            root_prefix = "/" + str(native.tag)
            stripped = selector[1:] if selector.startswith("//") else selector
            if stripped == root_prefix:
                return [node or self.root]
            if stripped.startswith(root_prefix + "/"):
                selector = "." + stripped[len(root_prefix):]
            elif selector.startswith("//"):
                selector = "." + selector
            else:
                raise ValueError(f"Absolute path must start with {root_prefix}; use .//Tag for descendants (case-sensitive)")
        try:
            selected = native.findall(selector) if self.kind == "xml" else native.select(selector)
        except Exception as exc:
            raise ValueError(f"Invalid {self.kind} selector: {exc}. XML uses case-sensitive .//Tag or ./Child; omit selector to inspect actual tags.") from exc
        return [self.by_native[id(x)] for x in selected]


def pointer(value, path):
    if path == "":
        return value
    if not path.startswith("/"):
        raise ValueError("Use an RFC 6901 JSON pointer starting with /")
    for key in path[1:].split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def brief(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= 600 else {"type": "string", "chars": len(value), "prefix": value[:250]}
    if depth > 4:
        return {"type": type(value).__name__}
    if isinstance(value, dict):
        return {k: brief(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "samples": [brief(v, depth + 1) for v in value[:3]]}
    return value


class DocumentTools:
    """Bounded declarative processing, without task/site adapters or executable code."""

    def __init__(self, config, web=None):
        self.config = config
        self.settings = config.get("documents", {})
        self.root = Path(self.settings.get("state_dir", "downloads/documents")).resolve()
        self.web = web or WebTools(config.get("web", {}))
        self.max_nodes = int(self.settings.get("max_nodes", 100000))
        self.max_records = int(self.settings.get("max_records", 10000))
        self.max_materialized_chars = int(self.settings.get("max_materialized_chars", min(128000000, 8 * self.web.max_bytes)))
        if not 100 <= self.max_nodes <= 200000 or not 1 <= self.max_records <= 10000:
            raise ValueError("Invalid document node/record budget")
        if not 1024 <= self.max_materialized_chars <= 128000000:
            raise ValueError("Invalid document materialized text budget")

    def _path(self, identity, group):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("Invalid document identity")
        path = (self.root / group / identity).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Document path escapes configured workspace")
        return path

    def _save_source(self, raw, metadata, kind=None):
        if len(raw) > self.web.max_bytes:
            raise ValueError("Decoded document exceeds web.max_response_bytes")
        if kind is None:
            prefix = raw.lstrip()[:100].lower()
            mime = metadata.get("content_type", "")
            kind = "json" if prefix.startswith((b"{", b"[")) else "html" if "html" in mime or b"<html" in prefix or b"<!doctype html" in prefix else "xml" if prefix.startswith(b"<") else "text"
        metadata = {**metadata, "format": kind, "sha256": digest(raw), "bytes": len(raw)}
        identity = digest(metadata)
        folder = self._path(identity, "sources")
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / "source.json").exists():
            (folder / "source.bin").write_bytes(raw)
            atomic_json(folder / "source.json", metadata)
        self._source(identity)
        return {"ok": True, "source_id": identity, **metadata,
                "artifact_paths": [str(folder / "source.bin"), str(folder / "source.json")]}

    def _source(self, identity):
        folder = self._path(identity, "sources")
        meta = json.loads((folder / "source.json").read_text(encoding="utf-8"))
        raw = (folder / "source.bin").read_bytes()
        if digest(meta) != identity or digest(raw) != meta["sha256"]:
            raise ValueError("Document source/hash changed")
        if len(raw) > self.web.max_bytes:
            raise ValueError("Document exceeds configured byte budget")
        if meta.get("parent_source_id"):
            parent_raw, parent = self._source(meta["parent_source_id"])
            value = pointer(json.loads(parent_raw), meta["json_pointer"])
            decoded = base64.b64decode(value, validate=True) if meta["encoding"] == "base64" else value.encode("utf-8")
            if decoded != raw:
                raise ValueError("Derived document differs from parent source")
        return raw, meta

    async def open(self, url: str) -> dict[str, Any]:
        if not self.settings.get("enabled", True):
            raise ValueError("Document tools are disabled")
        fetched = await self.web.fetch(url, max_chars=500)
        if not fetched.get("ok"):
            return fetched
        raw = Path(fetched["artifact_paths"][1]).read_bytes()
        if digest(raw) != fetched["response_sha256"]:
            raise ValueError("Fetched document hash changed")
        source = self._save_source(raw, {k: fetched[k] for k in (
            "url", "requested_url", "accessed_at", "content_type", "title") if k in fetched})
        source["network_route"] = fetched.get("network_route")
        source["inspection"] = await self.inspect(source["source_id"])
        return source

    async def decode(self, source_id: str, json_pointer: str, encoding: str = "base64", format: str = "xml") -> dict[str, Any]:
        raw, meta = self._source(source_id)
        if meta["format"] != "json" or encoding not in {"base64", "text"} or format not in {"xml", "html", "json", "text"}:
            raise ValueError("Decode requires a JSON source, base64/text encoding and supported format")
        value = pointer(json.loads(raw), json_pointer)
        if not isinstance(value, str):
            raise ValueError("Selected JSON value must be a string")
        decoded = base64.b64decode(value, validate=True) if encoding == "base64" else value.encode("utf-8")
        result = self._save_source(decoded, {"url": meta.get("url"), "parent_source_id": source_id,
            "json_pointer": json_pointer, "encoding": encoding, "content_type": "application/" + format}, kind=format)
        result["inspection"] = await self.inspect(result["source_id"])
        return result

    def _tree(self, raw, meta):
        if meta["format"] not in {"html", "xml"}:
            raise ValueError("Structural selectors require HTML or XML; decode embedded documents first")
        return DocumentTree(raw, meta["format"], meta.get("content_type", ""), self.max_nodes)

    async def inspect(self, source_id: str, selector: str | None = None, offset: int = 0, limit: int = 5) -> dict[str, Any]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not 1 <= limit <= 10:
            raise ValueError("Inspection offset must be nonnegative and limit 1..10")
        raw, meta = self._source(source_id)
        result = {"ok": True, "source_id": source_id, "format": meta["format"], "bytes": len(raw), "source": meta}
        if meta["format"] == "json":
            value = pointer(json.loads(raw), selector or "")
            return {**result, "json_pointer": selector or "", "value": brief(value)}
        if meta["format"] == "text":
            text = raw.decode("utf-8")
            return {**result, "text": text[offset:offset + 2000], "total_chars": len(text)}
        tree = await asyncio.to_thread(self._tree, raw, meta)
        nodes = tree.select(selector) if selector else [tree.root]
        if offset >= len(nodes) and nodes:
            return {**result, 'ok': False, 'status': 'offset_out_of_range', 'selector': selector,
                'total': len(nodes), 'message': 'Offset exceeds selected nodes. Pagination must repeat the SAME selector as the previous page; omit offset for the structure inventory.'}
        tags = Counter(n.tag for n in tree.nodes.values())
        selectors = Counter()
        for n in tree.nodes.values():
            if meta['format'] == 'xml':
                selectors['.//' + n.tag] += 1
            else:
                classes = n.attrs.get('class', [])
                for name in classes if isinstance(classes, list) else classes.split():
                    if re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', name):
                        selectors[n.tag + '.' + name] += 1
        if not nodes:
            return {**result, "ok": False, "status": "empty_selection", "total": 0, "samples": [],
                "root_tag": tree.root.tag, "tags": dict(tags.most_common(40)), "observed_selectors": dict(selectors.most_common(40)),
                "message": "No nodes matched. Do not repeat this query. Selectors are case-sensitive; XML descendants use .//Tag (replace Tag with an observed tag). Omit selector for structure inventory."}
        selected = nodes[offset:offset + limit]
        samples = [{"path": n.path, "tag": n.tag, "attributes": n.attrs,
                    "text": n.text().strip()[:1200], "text_chars": len(n.text()),
                    "text_preview_truncated": len(n.text().strip()) > 1200,
                    "children": dict(Counter(x.tag for x in n.content if isinstance(x, Node)))} for n in selected]
        result.update(selector_language="ElementTree XPath" if meta["format"] == "xml" else "CSS",
            selector=selector, total=len(nodes), offset=offset, next_offset=offset + limit if offset + limit < len(nodes) else None, samples=samples,
            next_page_arguments={'source_id': source_id, 'selector': selector, 'offset': offset + limit, 'limit': limit} if offset + limit < len(nodes) else None)
        if selector is None:
            classes = Counter(c for n in tree.nodes.values() for c in (n.attrs.get("class", []) if isinstance(n.attrs.get("class", []), list) else n.attrs.get("class", "").split()))
            result.update(tags=dict(tags.most_common(60)), classes=dict(classes.most_common(60)),
                observed_selectors=dict(selectors.most_common(50)),
                ids=[n.attrs["id"] for n in tree.nodes.values() if "id" in n.attrs][:40],
                selector_examples=[(".//" if meta["format"] == "xml" else "") + tag for tag, count in tags.most_common(12) if tag != "[document]"])
        return result

    def _transform(self, source_id, spec, engine=2):
        Draft202012Validator(SPEC_SCHEMA).validate(spec)
        if len(json.dumps(spec)) > 12000:
            raise ValueError("Document spec exceeds 12000 characters")
        if spec.get('key_source') and (spec.get('key_pattern') or spec.get('key_separator')):
            raise ValueError('Use key_transforms with key_source; legacy key_pattern/key_separator read heading text only')
        collection = spec.get("collection_key", "records")
        if collection in {"source", "source_id", "method", "coverage", "validation", "remainder", "fragments", "metadata", "schema_version", "source_unit_mapping"}:
            raise ValueError("Reserved collection key")
        if spec["mode"] == "sections" and not (spec.get("start_pattern") or spec.get("heading_selector")):
            raise ValueError("Sections require start_pattern or heading_selector")
        raw, meta = self._source(source_id)
        tree = self._tree(raw, meta)
        units = tree.select(spec["selector"])
        if not units:
            raise ValueError("Selector matched no nodes; inspect the document first")
        paths = {n.path for n in units}
        for node in units:
            parent = node.path.rsplit("/", 1)[0]
            while parent:
                if parent in paths:
                    raise ValueError("Selector contains overlapping ancestor/child nodes; select disjoint units")
                parent = parent.rsplit("/", 1)[0]
            if "/" in paths and node.path != "/":
                raise ValueError("Selector contains overlapping nodes")
        started = time.monotonic()
        patterns = {k: regex.compile(spec[k]) for k in ("start_pattern", "exclude_pattern", "key_pattern", "key_separator") if spec.get(k)}

        def match(key, value):
            if time.monotonic() - started > 15:
                raise ValueError("Document transformation exceeded time budget")
            return patterns[key].search(value, timeout=0.05) if key in patterns else None

        heading_paths = {n.path for n in tree.select(spec["heading_selector"])} if spec.get("heading_selector") else set()
        body_paths = {n.path for n in tree.select(spec["body_selector"])} if spec.get("body_selector") else None
        if spec['mode'] == 'sections' and heading_paths - paths:
            raise ValueError(f"heading_selector matched {len(heading_paths)} headings but {len(heading_paths - paths)} are absent from selector. selector must select the heading AND content units, e.g. 'h2.topic, p', NOT their enclosing body/div container.")
        if spec['mode'] == 'sections' and body_paths and body_paths - paths:
            raise ValueError("body_selector matches nodes absent from the unit selector. Include those content units in selector; body_selector partitions selected units, it does not add units.")
        groups, current = [], None
        for node in units:
            text = node.text().strip()
            if spec["mode"] == "elements":
                groups.append([node])
            elif match("exclude_pattern", text):
                # A boundary closes the previous group; its text remains in the remainder.
                current = None
            elif ((not spec.get('heading_selector') or node.path in heading_paths)
                  and (match('start_pattern', text) if spec.get('start_pattern') else node.path in heading_paths)):
                current = [node]
                groups.append(current)
            elif current is not None:
                current.append(node)
        if not groups:
            raise ValueError("No record boundaries matched; inspect samples and revise the rule")
        records, fragments, unmatched_keys, mappings = [], {}, [], []
        materialized_chars = 0
        for group in groups:
            first = group[0]
            title_nodes = tree.select(spec["title_selector"], first) if spec.get("title_selector") else [first]
            heading = "\n".join(n.text().strip() for n in title_nodes)
            key_match = match("key_pattern", heading)
            if 'key_pattern' in patterns and (key_match is None or not (key_match.group(1 if key_match.lastindex else 0) or '').strip()):
                unmatched_keys.append({"path": first.path, "heading": heading[:180]})
            key = (key_match.group(1) if key_match and key_match.lastindex else key_match.group(0) if key_match else heading) or heading
            keys = patterns["key_separator"].split(key, timeout=0.05) if "key_separator" in patterns else [key]
            keys = [k.strip() for k in keys if k.strip()] or [first.path]
            mapping, label_warnings = None, []
            if spec.get('key_source') or spec.get('key_transforms'):
                if engine < 2:
                    raise ValueError('This label pipeline requires document engine 2')
                keys, mapping, label_warnings = label_values(tree, first, heading, spec, self.max_records, started + 15)
            elif engine >= 2:
                mapping = {'source_path': first.path, 'attribute': None, 'input': heading,
                    'steps': [{'operation': 'legacy_capture_split', 'output': keys}], 'output_labels': keys}
            if len(records) + len(keys) > self.max_records:
                raise ValueError("Document record limit exceeded")
            body, extras = [], []
            if spec["mode"] == "elements":
                content = tree.select(spec["body_selector"], first) if spec.get("body_selector") else [first]
                body = [n.text().strip() for n in content]
            else:
                for node in group:
                    if body_paths is None or node.path in body_paths or node is first:
                        body.append(node.text().strip())
                    else:
                        extras.append({"path": node.path, "text": node.text().strip()})
            text = "\n".join(body)
            source_paths = [n.path for n in group]
            # Grouped labels duplicate the materialized record in JSON. Bound that
            # expansion before creating rows, independently of source byte limits.
            record_chars = (len(heading) + len(text) + sum(map(len, body)) + sum(map(len, source_paths))
                            + sum(len(a['path']) + len(a['text']) for a in extras) + 256)
            materialized_chars += len(keys) * record_chars + sum(map(len, keys))
            if mapping:
                materialized_chars += len(json.dumps(mapping, ensure_ascii=False))
            if materialized_chars > self.max_materialized_chars:
                raise ValueError("Document materialized text limit exceeded; reduce grouped expansion or configure max_materialized_chars")
            for node in group:
                fragments[node.path] = node.tree()
            for k in keys:
                records.append({"id": digest([source_id, first.path, k]), "key": k, "heading": heading,
                    "text": text, "text_sha256": digest(text.encode()), "paragraphs": body,
                    "source_paths": source_paths, "annotations": extras,
                    "shared_source": len(keys) > 1})
            if mapping:
                mappings.append({**mapping, 'record_start_path': first.path,
                    'record_indexes': list(range(len(records) - len(keys), len(records))),
                    'record_ids': [r['id'] for r in records[-len(keys):]], 'diagnostics': label_warnings})
        remainder = tree.root.tree(fragments)
        reconstructed = self._restore(remainder, fragments)
        if reconstructed != tree.root.tree():
            raise ValueError("Source tree conservation failed")
        remainder_tags = Counter()
        remainder_chars = 0

        def inspect_remainder(item):
            nonlocal remainder_chars
            if isinstance(item, dict) and "tag" in item:
                remainder_tags[item["tag"]] += 1
                for child in item["content"]:
                    inspect_remainder(child)
            elif isinstance(item, str):
                remainder_chars += len(item.strip())
        inspect_remainder(remainder)
        coverage = {"source_tree_sha256": digest(tree.root.tree()), "reconstructed_tree_sha256": digest(reconstructed),
            "tree_conserved": True, "selected_units": len(units), "assigned_units": len(fragments),
            "unassigned_selected_units": len(paths - fragments.keys()), "record_count": len(records),
            "empty_body_records": sum(not r["text"].strip() for r in records),
            "duplicate_keys": [k for k, count in Counter(r["key"] for r in records).items() if count > 1][:30],
            "remainder_tags": dict(remainder_tags.most_common(60)), "remainder_text_chars": remainder_chars,
            "semantic_completeness": "requires_review"}
        validation = {"ok": not unmatched_keys and len({r['id'] for r in records}) == len(records),
            "unmatched_key_count": len(unmatched_keys), "unmatched_key_samples": unmatched_keys[:8],
            "unique_ids": len({r['id'] for r in records}) == len(records)}
        result = {"schema_version": engine, "source_id": source_id, "source": meta,
            "metadata": {"origin": "planner_supplied", "values": spec.get("metadata", {})},
            "method": spec, collection: records, "fragments": fragments, "remainder": remainder, "coverage": coverage, "validation": validation}
        if engine >= 2:
            result['source_unit_mapping'] = mappings
        return result

    @staticmethod
    def _restore(item, fragments):
        if isinstance(item, dict) and "fragment_ref" in item:
            return fragments[item["fragment_ref"]]
        if isinstance(item, dict):
            return {k: DocumentTools._restore(v, fragments) for k, v in item.items()}
        if isinstance(item, list):
            return [DocumentTools._restore(v, fragments) for v in item]
        return item

    async def preview(self, source_id: str, spec: dict) -> dict[str, Any]:
        """Freeze a candidate; do not equate structural conservation with correct segmentation."""
        result = await asyncio.to_thread(self._transform, source_id, deepcopy(spec))
        job_id = digest({"source_id": source_id, "spec": spec, "engine": 2})
        folder = self._path(job_id, "jobs")
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / "receipt.json").exists():
            atomic_json(folder / "candidate.json", result)
            atomic_json(folder / "receipt.json", {"source_id": source_id, "spec": spec, "candidate_sha256": digest(result), "engine": 2})
        return await self.review(job_id)

    def _job(self, job_id):
        folder = self._path(job_id, "jobs")
        receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
        if digest({k: receipt[k] for k in ("source_id", "spec", "engine")}) != job_id:
            raise ValueError("Job recipe/hash changed")
        if receipt['engine'] not in {1, 2}:
            raise ValueError('Unsupported document engine version')
        candidate = json.loads((folder / "candidate.json").read_text(encoding="utf-8"))
        if digest(candidate) != receipt["candidate_sha256"]:
            raise ValueError("Candidate/hash changed")
        self._source(receipt["source_id"])
        return folder, receipt, candidate

    @staticmethod
    def key_diagnostics(spec, records, mappings=()):
        """Expose a detectable capture/split mismatch without guessing record labels."""
        warnings = []
        for mapping in mappings:
            for warning in mapping.get('diagnostics', []):
                warnings.append({**warning, 'record_index': mapping['record_indexes'][0], 'source_path': mapping['source_path']})
        if warnings:
            return {'warnings': warnings[:8], 'truncated': len(warnings) > 8}
        if not spec.get('key_pattern') or not spec.get('key_separator'):
            return {'warnings': warnings, 'truncated': False}
        capture, separator = regex.compile(spec['key_pattern']), regex.compile(spec['key_separator'])
        started = time.monotonic()
        seen = set()
        for i, row in enumerate(records):
            path = row['source_paths'][0]
            if path in seen:
                continue
            seen.add(path)
            if len(warnings) >= 8 or time.monotonic() - started > 3:
                return {'warnings': warnings, 'truncated': True}
            match = capture.search(row['heading'], timeout=0.05)
            if match is None:
                continue
            group = 1 if match.lastindex else 0
            suffix = row['heading'][match.end(group):]
            delimiter = separator.match(suffix, timeout=0.05)
            if delimiter and delimiter.end() > 0:
                warnings.append({'record_index': i, 'extracted_key': row['key'][:240],
                    'unconsumed_suffix': suffix[:240],
                    'message': 'key_separator matches just outside the extracted label. It only splits the captured label; inspect whether key_pattern omitted part of a grouped label.'})
        return {'warnings': warnings, 'truncated': False}

    async def review(self, job_id: str, offset: int = 0, limit: int = 3) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 10:
            raise ValueError("Review offset must be nonnegative and limit 1..10")
        folder, receipt, candidate = self._job(job_id)
        records = candidate[receipt["spec"].get("collection_key", "records")]
        indexes = list(range(offset, min(len(records), offset + limit)))
        if offset == 0:
            indexes = sorted(set(indexes + [len(records) // 2, len(records) - 1]))
        return {"ok": True, "job_id": job_id, "source_id": receipt["source_id"], "status": "exported" if (folder / "export.json").exists() else "preview",
            "coverage": candidate["coverage"], "validation": candidate["validation"], "record_count": len(records),
            "key_diagnostics": self.key_diagnostics(receipt['spec'], records, candidate.get('source_unit_mapping', [])),
            "label_mapping_samples": [m for m in candidate.get('source_unit_mapping', []) if any(i in indexes for i in m['record_indexes'])],
            "record_fields": list(records[0]), "source": candidate['source'],
            "samples": [{"index": i, **{k: records[i][k] for k in ("key", "heading", "source_paths", "shared_source")},
                "text": records[i]["text"][:1400], "text_chars": len(records[i]["text"]),
                "text_preview_truncated": len(records[i]['text']) > 1400,
                "paragraph_count": len(records[i]["paragraphs"]), "annotation_count": len(records[i]["annotations"]),
                "annotation_samples": [{**a, 'text': a['text'][:300], 'text_chars': len(a['text']),
                    'text_preview_truncated': len(a['text']) > 300} for a in records[i]['annotations'][:2]]} for i in indexes],
            "next_offset": offset + limit if offset + limit < len(records) else None,
            "message": "Revise the recipe when validation.ok=false; export is blocked. Check whether unmatched headings are non-record structure or the key pattern is wrong. Check boundaries, body vs annotations, source metadata and remainder. tree_conserved is not proof of semantic completeness."}

    async def export(self, job_id: str) -> dict[str, Any]:
        folder, receipt, candidate = self._job(job_id)
        if not candidate['validation']['ok']:
            raise ValueError('Recipe constraints failed; revise document_preview before export: ' + json.dumps(candidate['validation'], ensure_ascii=False))
        # Independently replay the frozen transformation on verified full source bytes.
        replay = await asyncio.to_thread(self._transform, receipt["source_id"], receipt["spec"], receipt['engine'])
        if digest(replay) != receipt["candidate_sha256"]:
            raise ValueError("Independent transformation replay differs")
        path = folder / "export.json"
        if path.exists():
            if digest(json.loads(path.read_text(encoding="utf-8"))) != receipt["candidate_sha256"]:
                raise ValueError("Existing export changed; refusing to overwrite")
        else:
            atomic_json(path, candidate)
        verification = {"source_id": receipt["source_id"], "job_id": job_id, "replay_matches": True,
            "output_sha256": digest(path.read_bytes()), "coverage": candidate["coverage"],
            "method_sha256": digest(receipt["spec"]), "semantic_completeness": "requires_review"}
        atomic_json(folder / "verification.json", verification)
        return {"ok": True, "status": "exported", **verification, "record_count": candidate["coverage"]["record_count"],
            "artifact_paths": [str(path), str(folder / "verification.json")],
            "output_path": str(path)}

    def records(self, job_id):
        """Bridge a complete verified export to an existing processing worker by ID."""
        folder, receipt, candidate = self._job(job_id)
        if not candidate.get('validation', {}).get('ok'):
            raise ValueError('Document candidate has not passed recipe validation')
        published = json.loads((folder / 'export.json').read_text(encoding='utf-8'))
        if digest(published) != receipt['candidate_sha256']:
            raise ValueError('Document export/hash changed')
        return [{'source_url': candidate['source'].get('url', ''),
                 'data': {**record, 'document_source_id': receipt['source_id'], 'document_job_id': job_id}}
                for record in published[receipt['spec'].get('collection_key', 'records')]]

    def definitions(self):
        identity = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        paging = {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}
        return [
            Tool("document_open", "Download a full public HTML/XML/JSON/text source to an immutable source_id and inspect its structure. Reuses web proxy/size/access rules; never transcribe long sources through model context.", schema({"url": {"type": "string"}}, ["url"]), self.open),
            Tool("document_inspect", "Inspect full saved source by ID: HTML CSS, XML ElementTree XPath, JSON pointer. Omit selector for structure inventory. Bounded samples have total counts; select headers/paragraphs to infer a general extraction recipe.", schema({"source_id": identity, "selector": {"type": "string", "maxLength": 500}, **paging}, ["source_id"]), self.inspect, read_only=True),
            Tool("document_decode", "Derive a complete embedded document from a JSON string field (JSON pointer), using base64 or text. Retains and verifies parent provenance. Does not execute content.", schema({"source_id": identity, "json_pointer": {"type": "string", "maxLength": 500}, "encoding": {"enum": ["base64", "text"]}, "format": {"enum": ["xml", "html", "json", "text"]}}, ["source_id", "json_pointer"]), self.decode),
            Tool("document_preview", "Test a planner-authored recipe on full source. elements selects complete disjoint nodes; sections selects headings AND content units; heading_selector/start_pattern start records, exclude_pattern closes groups, body_selector separates annotations. Read labels with key_source (relative selector and/or attribute), then key_transforms capture/split/integer_range. Or use legacy key_pattern/key_separator. The pipeline is explicit and source-bound; labels never rewrite bodies. CSS for HTML, ElementTree XPath for XML. Preserves full fragments, remainder and source-to-record mapping. Agent tasks separately review semantics before export; rejected candidates require effective revision.", schema({"source_id": identity, "spec": SPEC_SCHEMA}, ["source_id", "spec"]), self.preview),
            Tool("document_review", "Read preview/export samples and coverage by job_id. Duplicate labels can be legitimate in separate source scopes; inspect source_paths and remainder. Counts alone do not prove completeness.", schema({"job_id": identity, **paging}, ["job_id"]), self.review, read_only=True),
            Tool("document_export", "Export a reviewed candidate to JSON after independent full-source replay and hash verification. Uses frozen job_id; returns real file paths. Idempotent across restarts, no model rewriting. Semantic correctness remains a review obligation.", schema({"job_id": identity}, ["job_id"]), self.export),
        ]
