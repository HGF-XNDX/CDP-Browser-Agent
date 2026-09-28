from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from jsonschema.validators import validator_for
from pathlib import Path
from uuid import uuid4
from .artifacts import ArtifactStore


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[Any]]
    output_schema: dict | None = None
    read_only: bool = False


class ToolRegistry:
    """Only host-registered tools can run. No model-directed imports or shell."""

    def __init__(self, timeout: float = 60, max_result_chars: int = 12000, artifact_store: ArtifactStore | None = None):
        if not math.isfinite(timeout) or timeout <= 0 or max_result_chars < 512:
            raise ValueError("tool timeout must be positive and result budget >= 512")
        self.timeout = timeout
        self.max_result_chars = max_result_chars
        self._tools: dict[str, Tool] = {}
        self.artifacts = artifact_store or ArtifactStore(Path("downloads/tool-results") / uuid4().hex)

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", tool.name):
            raise ValueError(f"Invalid tool name: {tool.name}")
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        def check_refs(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"$ref", "$dynamicRef"} and isinstance(item, str) and not item.startswith("#"):
                        raise ValueError("Tool schemas must be self-contained; remote references are not fetched")
                    check_refs(item)
            elif isinstance(value, list):
                for item in value:
                    check_refs(item)
        check_refs(tool.input_schema)
        validator_for(tool.input_schema).check_schema(tool.input_schema)
        if tool.output_schema is not None:
            check_refs(tool.output_schema)
            validator_for(tool.output_schema).check_schema(tool.output_schema)
        self._tools[tool.name] = tool

    def catalog(self, query: str = "", offset: int = 0, limit: int = 30) -> dict:
        matches = [t for t in self._tools.values()
                   if query.lower() in (t.name + " " + t.description).lower()]
        limit = max(1, min(50, limit))
        offset = max(0, offset)
        return {"tools": [{"name": t.name, "description": t.description[:400]}
                          for t in matches[offset:offset + limit]],
                "total": len(matches),
                "next_offset": offset + limit if offset + limit < len(matches) else None}

    def describe(self, name: str) -> dict:
        tool = self._tools[name]
        return {"name": tool.name, "description": tool.description,
                "input_schema": tool.input_schema, "output_schema": tool.output_schema,
                "read_only": tool.read_only}

    def validate(self, name: str, arguments: dict) -> None:
        if name not in self._tools:
            raise ValueError(f"Unknown or disallowed tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        schema = self._tools[name].input_schema
        validator_for(schema)(schema).validate(arguments)

    async def call(self, name: str, arguments: dict) -> dict:
        try:
            self.validate(name, arguments)
            value = await asyncio.wait_for(self._tools[name].handler(**arguments), self.timeout)
            output = self._tools[name].output_schema
            if output:
                validator_for(output)(output).validate(value)
            result = value if isinstance(value, dict) and "ok" in value else {"ok": True, "data": value}
            encoded = json.dumps(result, ensure_ascii=False, default=str)
            if len(encoded) > self.max_result_chars:
                reference = self.artifacts.save(result)
                # Keep machine-readable source identity; a JSON preview is not a tool result.
                compact = {k: result[k] for k in ("ok", "status", "url", "needs_browser", "browser_url",
                    "artifact_paths", "artifact_id", "artifact", "text_sha256", "response_sha256", "offset", "next_offset", "total_chars") if k in result}
                compact.update(truncated=True, original_chars=len(encoded), artifact=reference,
                               message="Full result saved. Use artifact_read or artifact_search for omitted evidence.")
                spare = self.max_result_chars - len(json.dumps(compact, ensure_ascii=False)) - 40
                if spare > 0:
                    if isinstance(result.get("text"), str):
                        compact["text"] = result["text"][:spare // 2]
                        compact["next_offset"] = result.get("offset", 0) + len(compact["text"])
                    else:
                        compact["preview"] = encoded[:spare // 2]
                elif "text" in result:
                    compact["next_offset"] = result.get("offset", 0)
                return compact  # Identity metadata is allowed to exceed a very small budget.
            return result
        except asyncio.TimeoutError:
            return {"ok": False, "errorType": "tool_timeout",
                    "message": "Tool timed out; side effects may have occurred. Verify before retrying."}
        except Exception as exc:
            return {"ok": False, "errorType": type(exc).__name__, "message": str(exc)[:2000]}
