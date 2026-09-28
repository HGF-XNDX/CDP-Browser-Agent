from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from .verification import validate_checks


def local_schema(schema):
    Draft202012Validator.check_schema(schema)
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"}:
                    raise ValueError("Processing schemas must be inline without references")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(schema)


class ProcessingCatalog:
    def __init__(self, paths=()):
        self.profiles = {}
        for root in map(Path, paths):
            if not root.exists():
                raise ValueError(f"Processing profile path does not exist: {root}")
            for file in ([root] if root.is_file() else sorted(root.glob("*.json"))):
                if file.stat().st_size > 256000:
                    raise ValueError("Processing profile exceeds 256 KB")
                profile = json.loads(file.read_text(encoding="utf-8-sig"))
                name = profile.get("name", "")
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name in self.profiles:
                    raise ValueError("Invalid or duplicate processing profile name")
                if profile.get("mode", "llm") not in {"llm", "mapping"}:
                    raise ValueError("Processing mode must be llm or mapping")
                validate_checks(profile.get("verification", []))
                local_schema(profile["output_schema"])
                if profile["output_schema"].get("type") != "object":
                    raise ValueError("Processing output schema must describe an object")
                if not isinstance(profile.get("instructions"), str) or not profile["instructions"].strip():
                    raise ValueError("Processing instructions are required")
                if not set(profile.get("formats", ["json", "csv", "markdown"])) <= {"json", "csv", "markdown"}:
                    raise ValueError("Supported exports: json, csv, markdown")
                for key in ("skills", "evidence_fields"):
                    if not isinstance(profile.get(key, []), list) or not all(isinstance(v, str) for v in profile.get(key, [])):
                        raise ValueError(f"Processing {key} must be a list of strings")
                if not isinstance(profile.get("require_evidence", True), bool):
                    raise ValueError("require_evidence must be a boolean")
                for key, default, lo, hi in (("max_input_chars", 60000, 1000, 200000),
                                            ("max_records", 500, 1, 10000), ("max_repairs", 1, 0, 3),
                                            ("timeout_seconds", 300, 1, 3600)):
                    value = profile.get(key, default)
                    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                        raise ValueError(f"Invalid processing {key}")
                self.profiles[name] = profile

    def get(self, name):
        if name not in self.profiles:
            raise ValueError(f"Unknown operator-configured processing profile: {name}")
        return deepcopy(self.profiles[name])

    def catalog(self):
        return [{"name": p["name"], "description": p.get("description", ""),
                 "formats": p.get("formats", ["json", "csv", "markdown"]), "output_schema": p["output_schema"],
                 "verification": p.get("verification", [])}
                for p in self.profiles.values()]
