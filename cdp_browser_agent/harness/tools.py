from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from jsonschema.validators import validator_for


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[Any]]


class ToolRegistry:
    """Only host-registered tools can run. No model-directed imports or shell."""

    def __init__(self, timeout: float = 60, max_result_chars: int = 12000):
        if not math.isfinite(timeout) or timeout <= 0 or max_result_chars < 512:
            raise ValueError("tool timeout must be positive and result budget >= 512")
        self.timeout = timeout
        self.max_result_chars = max_result_chars
        self._tools: dict[str, Tool] = {}

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
                "input_schema": tool.input_schema}

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
            result = value if isinstance(value, dict) and "ok" in value else {"ok": True, "data": value}
            encoded = json.dumps(result, ensure_ascii=False, default=str)
            if len(encoded) > self.max_result_chars:
                return {"ok": bool(result.get("ok")), "truncated": True,
                        "original_chars": len(encoded), "preview": encoded[:self.max_result_chars],
                        "message": "Result exceeded context budget; narrow the query."}
            return result
        except asyncio.TimeoutError:
            return {"ok": False, "errorType": "tool_timeout",
                    "message": "Tool timed out; side effects may have occurred. Verify before retrying."}
        except Exception as exc:
            return {"ok": False, "errorType": type(exc).__name__, "message": str(exc)[:2000]}
