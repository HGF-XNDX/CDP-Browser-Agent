from __future__ import annotations

import os
import re
from contextlib import AsyncExitStack

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from .skills import SkillCatalog
from .tools import Tool, ToolRegistry
from ..web.tools import WebTools


def object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


class ExtensionRuntime:
    def __init__(self, config: dict, tools: list[Tool] | None = None):
        settings = config.get("harness", {})
        self.settings = settings
        self.registry = ToolRegistry(float(settings.get("tool_timeout_seconds", 60)),
                                     int(settings.get("max_tool_result_chars", 12000)))
        self.skills = SkillCatalog(settings.get("skill_paths", []),
                                   int(settings.get("max_skill_chars", 20000)),
                                   int(settings.get("active_skill_budget_chars", 30000)))
        self.stack = AsyncExitStack()
        self.web = WebTools(config.get("web", {}), self.registry.max_result_chars)
        self._register_builtins()
        self.web_tool_names = []
        if config.get("web", {}).get("enabled", True):
            for tool in self.web.definitions():
                self.registry.register(tool)
                self.web_tool_names.append(tool.name)
        for tool in tools or []:
            self.registry.register(tool)
        for name in settings.get("active_skills", []):
            self.skills.load(name)

    def _register_builtins(self) -> None:
        text = {"type": "string"}
        paging = {"query": text, "offset": {"type": "integer", "minimum": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 50}}

        async def skill_list(**args):
            return self.skills.catalog(**args)

        async def skill_load(name):
            return self.skills.load(name)

        async def skill_unload(name):
            self.skills.active.pop(name, None)
            return {"unloaded": name}

        async def skill_read(**args):
            return self.skills.read(**args)

        async def tool_list(**args):
            return self.registry.catalog(**args)

        async def tool_describe(name):
            return self.registry.describe(name)

        definitions = [
            Tool("skill_list", "Find available skills by name/description; paginated metadata only.", object_schema(paging), skill_list),
            Tool("skill_load", "Load a skill's instructions into active context.", object_schema({"name": text}, ["name"]), skill_load),
            Tool("skill_unload", "Release an unused skill from active context.", object_schema({"name": text}, ["name"]), skill_unload),
            Tool("skill_read", "Read a UTF-8 resource within an active skill. Does not execute scripts.",
                 object_schema({"name": text, "path": text, "offset": {"type": "integer", "minimum": 0},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, ["name", "path"]), skill_read),
            Tool("tool_list", "Search available external and built-in tools; paginated metadata only.", object_schema(paging), tool_list),
            Tool("tool_describe", "Get a tool's argument JSON Schema before calling it.", object_schema({"name": text}, ["name"]), tool_describe),
        ]
        self.builtin_names = [t.name for t in definitions]
        for tool in definitions:
            self.registry.register(tool)

    async def __aenter__(self):
        try:
            servers = self.settings.get("mcp_servers", {})
            if not isinstance(servers, dict):
                raise ValueError("harness.mcp_servers must be an object")
            for name, settings in servers.items():
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name):
                    raise ValueError(f"Invalid MCP server name: {name}")
                if not isinstance(settings.get("allow_tools"), list) or not all(isinstance(x, str) for x in settings["allow_tools"]):
                    raise ValueError(f"MCP server {name} requires an explicit allow_tools list")
                # An empty allowlist disables the server without launching a process.
                if not settings["allow_tools"]:
                    continue
                transport = settings.get("transport", "stdio")
                if transport == "stdio":
                    env = dict(settings.get("env", {}))
                    for variable in settings.get("env_from", []):
                        env[variable] = os.environ[variable]
                    target = StdioServerParameters(command=settings["command"], args=settings.get("args", []),
                                                   cwd=settings.get("cwd"), env=env)
                elif transport == "streamable-http":
                    headers = {header: os.environ[variable] for header, variable in settings.get("headers_from_env", {}).items()}
                    http = await self.stack.enter_async_context(httpx2.AsyncClient(
                        headers=headers, timeout=self.registry.timeout, trust_env=False))
                    target = streamable_http_client(settings["url"], http_client=http)
                else:
                    raise ValueError(f"Unsupported MCP transport: {transport}")
                client = await self.stack.enter_async_context(Client(target, read_timeout_seconds=self.registry.timeout))
                cursor = None
                seen_cursors = set()
                for _ in range(100):
                    page = await client.list_tools(cursor=cursor)
                    for remote in page.tools:
                        if remote.name not in settings["allow_tools"]:
                            continue
                        self._register_remote(name, remote, client)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                    if cursor in seen_cursors:
                        raise ValueError(f"Repeated tools/list cursor from {name}")
                    seen_cursors.add(cursor)
                else:
                    raise ValueError(f"Too many tools/list pages from {name}")
                # Validate against the complete registry, not only its first catalog page.
                for allowed in settings["allow_tools"]:
                    self.registry.describe(f"mcp.{name}.{allowed}")
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    def _register_remote(self, server, remote, client):
        async def invoke(**arguments):
            result = await client.call_tool(remote.name, arguments)
            # Never feed binary/base64 content into a text planner.
            content = [{"type": block.type, **({"text": block.text} if block.type == "text" else {"omitted": True})}
                       for block in result.content]
            return {"ok": not result.is_error, "content": content,
                    "structured_content": result.structured_content}
        self.registry.register(Tool(f"mcp.{server}.{remote.name}", remote.description or remote.name,
                                    remote.input_schema, invoke))

    async def __aexit__(self, *exc):
        return await self.stack.__aexit__(*exc)

    def context(self) -> dict:
        return {"builtin_tools": [self.registry.describe(n) for n in self.builtin_names + self.web_tool_names],
                "skills": self.skills.catalog(limit=15),
                "active_skills": list(self.skills.active.values()),
                "external_tool_count": len(self.registry._tools) - len(self.builtin_names) - len(self.web_tool_names)}
