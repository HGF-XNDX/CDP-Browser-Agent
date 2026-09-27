import asyncio
import socket
import sys
from pathlib import Path

import httpx

from cdp_browser_agent.harness.runtime import ExtensionRuntime


async def test_streamable_http_external_client(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "server.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).parent / "fixtures" / "external_mcp.py"), "--port", str(port), stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}/mcp"
            async with httpx.AsyncClient(trust_env=False, timeout=.3) as client:
                for _ in range(50):
                    try:
                        await client.get(url)
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(.1)
                else:
                    raise AssertionError("HTTP fixture did not start")
            config = {"harness": {"mcp_servers": {"remote": {"transport": "streamable-http", "url": url, "allow_tools": ["add"]}}}}
            async with ExtensionRuntime(config) as runtime:
                result = await runtime.registry.call("mcp.remote.add", {"a": 3, "b": 4})
                assert result["ok"] and result["structured_content"]["sum"] == 7, result
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 10)
