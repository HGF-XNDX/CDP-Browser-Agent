from mcp.server import MCPServer
from typing import Any

server = MCPServer("Test external tools")

@server.tool()
async def add(a: int, b: int) -> dict[str, Any]:
    return {"sum": a + b}

@server.tool()
async def fail() -> dict:
    raise ValueError("deliberate fixture failure")

@server.tool()
async def forbidden() -> dict:
    raise RuntimeError("allowlist failed")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.port:
        server.run("streamable-http", host="127.0.0.1", port=args.port)
    else:
        server.run()
