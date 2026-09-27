"""A small external MCP server for trying the extension interface locally."""
from mcp.server import MCPServer

server = MCPServer("Text toolbox")

@server.tool()
async def normalize_text(text: str) -> dict[str, str | int]:
    """Normalize whitespace in extracted page text and count its words."""
    normalized = " ".join(text.split())
    return {"text": normalized, "words": len(normalized.split())}

if __name__ == "__main__":
    server.run()
