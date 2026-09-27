"""Extension and execution boundaries shared by the CLI, Python API, and MCP."""

from .tools import Tool, ToolRegistry

__all__ = ["Tool", "ToolRegistry"]
