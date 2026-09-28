from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urldefrag, urlsplit

from jsonschema import Draft202012Validator


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 12000}
IDENTIFIER = {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$"}
FIELD = obj({"selector": {"type": "string", "maxLength": 500}, "attribute": TEXT,
             "required": {"type": "boolean"}, "url": {"type": "boolean"}})
FIELDS = {"type": "object", "minProperties": 1, "maxProperties": 32, "propertyNames": IDENTIFIER, "additionalProperties": FIELD}
CHECK = {"oneOf": [obj({"kind": {"const": "url"}, "prefix": TEXT}, ["kind", "prefix"]),
    obj({"kind": {"const": "visible"}, "selector": TEXT, "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["kind", "selector"]),
    obj({"kind": {"const": "text"}, "selector": TEXT, "contains": TEXT, "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["kind", "selector", "contains"])]}
COMMON = {"id": IDENTIFIER, "description": TEXT}
SCHEMA = obj({
    "schema_version": {"const": 1}, "name": IDENTIFIER, "version": TEXT, "description": TEXT,
    "parameters": {"type": "object"},
    "allowed_origins": {"type": "array", "minItems": 1, "maxItems": 20, "items": TEXT},
    "min_records": {"type": "integer", "minimum": 0, "maximum": 100000},
    "request_delay_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
    "steps": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"oneOf": [
        obj({**COMMON, "type": {"const": "navigate"}, "url": TEXT, "wait_for": TEXT}, ["id", "type", "url"]),
        obj({**COMMON, "type": {"const": "search"}, "query": TEXT,
             "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, ["id", "type", "query"]),
        obj({**COMMON, "type": {"const": "fetch"}, "urls": {"type": "array", "minItems": 1, "maxItems": 100, "items": TEXT},
             "input_step": IDENTIFIER, "url_field": IDENTIFIER, "browser_fallback": {"type": "boolean"},
             "on_error": {"enum": ["stop", "continue"]}}, ["id", "type"]),
        obj({**COMMON, "type": {"const": "process"}, "input_step": IDENTIFIER, "profile": IDENTIFIER}, ["id", "type", "input_step", "profile"]),
        obj({**COMMON, "type": {"const": "agent"}, "instructions": TEXT,
             "checks": {"type": "array", "items": CHECK, "minItems": 1, "maxItems": 20},
             "skills": {"type": "array", "items": TEXT, "maxItems": 20},
             "allow_tools": {"type": "array", "items": TEXT, "maxItems": 50},
             "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
             "replay_safe": {"type": "boolean"}}, ["id", "type", "instructions", "checks"]),
        obj({**COMMON, "type": {"const": "crawl"}, "start_url": TEXT, "wait_for": TEXT,
             "item_selector": TEXT, "fields": FIELDS,
             "key_fields": {"type": "array", "minItems": 1, "items": IDENTIFIER},
             "empty_selector": TEXT, "next_selector": TEXT,
             "max_pages": {"type": "integer", "minimum": 1, "maximum": 500},
             "max_records": {"type": "integer", "minimum": 1, "maximum": 10000},
             "on_limit": {"enum": ["incomplete", "complete"]},
             "record_schema": {"type": "object"},
             "follow": obj({"url_field": IDENTIFIER, "fields": FIELDS, "wait_for": TEXT}, ["url_field", "fields"])
            }, ["id", "type", "item_selector", "fields", "key_fields"])
    ]}}
}, ["schema_version", "name", "version", "description", "allowed_origins", "steps"])


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Workflow URLs must use http/https without embedded credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname.lower()}:{port}"


def scoped_url(url: str, allowed: list[str]) -> str:
    if origin(url) not in {origin(item) for item in allowed}:
        raise ValueError(f"URL outside workflow allowed_origins: {url}")
    return urldefrag(url)[0]


def validate_spec(spec: dict):
    Draft202012Validator(SCHEMA).validate(spec)
    ids = [step["id"] for step in spec["steps"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Workflow step IDs must be unique")
    parameters = spec.get("parameters", obj({}))
    Draft202012Validator.check_schema(parameters)
    if parameters.get("type") != "object" or parameters.get("additionalProperties") is not False:
        raise ValueError("parameters must be an object schema with additionalProperties=false")
    # Templates accept scalars only; remote schema resolution is never performed.
    for field in parameters.get("properties", {}).values():
        if field.get("type") not in {"string", "integer", "number", "boolean"}:
            raise ValueError("Workflow parameters must have scalar types")
    def refs(value):
        if isinstance(value, dict):
            if any(key in value for key in ("$ref", "$dynamicRef")):
                raise ValueError("Workflow parameter schemas cannot contain references")
            for child in value.values():
                refs(child)
        elif isinstance(value, list):
            for child in value:
                refs(child)
    refs(parameters)
    for step in spec["steps"]:
        if step.get("input_step") and step["input_step"] not in ids[:ids.index(step["id"])]:
            raise ValueError("input_step must refer to a preceding step")
        if step["type"] == "fetch" and bool(step.get("urls")) == bool(step.get("input_step")):
            raise ValueError("fetch requires exactly one of urls or input_step")
        if step["type"] == "crawl":
            if step.get("record_schema"):
                from ..processing.catalog import local_schema
                local_schema(step["record_schema"])
            fields = {**step["fields"], **step.get("follow", {}).get("fields", {})}
            if set(step["fields"]) & set(step.get("follow", {}).get("fields", {})):
                raise ValueError("Detail fields must not overwrite list fields")
            if any(key not in fields for key in step["key_fields"]):
                raise ValueError("key_fields must refer to extracted fields")
            if "follow" in step and step["follow"]["url_field"] not in step["fields"]:
                raise ValueError("follow.url_field must refer to a list field")


def render(spec: dict, parameters: dict | None) -> tuple[dict, dict]:
    validate_spec(spec)
    schema = spec.get("parameters", obj({}))
    values = {name: field["default"] for name, field in schema.get("properties", {}).items() if "default" in field}
    values.update(parameters or {})
    Draft202012Validator(schema).validate(values)
    def expand(value):
        if isinstance(value, str):
            def replace(match):
                key = match.group(1)
                if key not in values:
                    raise ValueError(f"Missing workflow parameter: {key}")
                text = str(values[key])
                return quote(text, safe="") if match.group(2) else text
            return re.sub(r"\$\{([a-zA-Z0-9_-]+)(:urlencode)?\}", replace, value)
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value
    concrete = expand(deepcopy(spec))
    validate_spec(concrete)
    for url in concrete["allowed_origins"]:
        parts = urlsplit(url)
        origin(url)
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ValueError("allowed_origins must contain origins, not paths or queries")
    return concrete, values


class WorkflowCatalog:
    def __init__(self, paths=()):
        self.items = {}
        for root in map(Path, paths):
            if not root.exists():
                raise ValueError(f"Configured workflow path does not exist: {root}")
            files = [root] if root.is_file() else sorted(root.glob("*.json"))
            for path in files:
                if path.stat().st_size > 256000:
                    raise ValueError(f"Workflow file too large: {path}")
                spec = json.loads(path.read_text(encoding="utf-8-sig"))
                validate_spec(spec)
                if spec["name"] in self.items:
                    raise ValueError(f"Duplicate workflow name: {spec['name']}")
                self.items[spec["name"]] = spec

    def get(self, name):
        if name not in self.items:
            raise ValueError(f"Unknown configured workflow: {name}")
        return deepcopy(self.items[name])

    def catalog(self):
        return [{"name": s["name"], "version": s["version"], "description": s["description"],
                 "parameters": s.get("parameters", obj({})), "steps": [{"id": step["id"], "type": step["type"]} for step in s["steps"]]}
                for s in self.items.values()]
