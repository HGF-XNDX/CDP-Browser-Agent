import asyncio
import sys
from pathlib import Path

import pytest

from cdp_browser_agent.harness.skills import SkillCatalog
from cdp_browser_agent.harness.tools import Tool, ToolRegistry
from cdp_browser_agent.harness.runtime import ExtensionRuntime


def make_skill(tmp_path, name="sample"):
    root = tmp_path / name
    root.mkdir()
    (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Extract page facts.\n---\nRead the source before answering.", encoding="utf-8")
    (root / "reference.txt").write_text("observed fact", encoding="utf-8")
    return root


def test_progressive_skill_loading_and_containment(tmp_path):
    make_skill(tmp_path)
    (tmp_path / "private.txt").write_text("private", encoding="utf-8")
    skills = SkillCatalog([str(tmp_path)])
    assert not skills.active
    assert "instructions" not in skills.catalog()["skills"][0]
    with pytest.raises(ValueError, match="Load the skill"):
        skills.read("sample", "reference.txt")
    assert "Read the source" in skills.load("sample")["instructions"]
    assert skills.read("sample", "reference.txt")["text"] == "observed fact"
    for escape in ("../private.txt", str(tmp_path / "private.txt")):
        with pytest.raises(ValueError, match="inside"):
            skills.read("sample", escape)


def test_invalid_and_duplicate_skills_fail_explicitly(tmp_path):
    root = make_skill(tmp_path)
    with pytest.raises(ValueError, match="Duplicate"):
        SkillCatalog([str(root), str(tmp_path)])
    (root / "SKILL.md").write_text("---\nname: wrong\ndescription: bad\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError, match="name/directory"):
        SkillCatalog([str(root)])


async def test_tool_validation_timeout_errors_and_truncation():
    calls = []
    async def double(value):
        calls.append(value)
        return {"value": value * 2}
    registry = ToolRegistry(timeout=.02, max_result_chars=512)
    registry.register(Tool("double", "Double an integer", {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}, double))
    assert (await registry.call("double", {"value": 3}))["data"]["value"] == 6
    assert not (await registry.call("double", {"value": "three"}))["ok"]
    assert not (await registry.call("missing", {}))["ok"]
    assert calls == [3]
    async def wait():
        await asyncio.sleep(10)
    registry.register(Tool("wait", "Wait", {"type": "object"}, wait))
    assert (await registry.call("wait", {}))["errorType"] == "tool_timeout"
    async def large():
        return {"text": "x" * 5000}
    registry.register(Tool("large", "Large", {"type": "object"}, large))
    assert (await registry.call("large", {}))["truncated"]


async def test_runtime_builtins_and_python_extension(tmp_path):
    make_skill(tmp_path)
    async def custom(value):
        return value.upper()
    config = {"harness": {"skill_paths": [str(tmp_path)]}}
    async with ExtensionRuntime(config, [Tool("custom", "uppercase", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}, custom)]) as runtime:
        assert not runtime.context()["active_skills"]
        assert (await runtime.registry.call("skill_load", {"name": "sample"}))["ok"]
        assert runtime.context()["active_skills"][0]["name"] == "sample"
        assert (await runtime.registry.call("custom", {"value": "abc"}))["data"] == "ABC"


async def test_real_stdio_external_mcp_allowlist():
    config = {"harness": {"mcp_servers": {"fixture": {"command": sys.executable,
              "args": [str(Path(__file__).parent / "fixtures" / "external_mcp.py")],
              "allow_tools": ["add", "fail"]}}}}
    async with ExtensionRuntime(config) as runtime:
        result = await runtime.registry.call("mcp.fixture.add", {"a": 2, "b": 5})
        assert result["ok"], result
        assert result["structured_content"]["sum"] == 7
        assert not (await runtime.registry.call("mcp.fixture.fail", {}))["ok"]
        assert not (await runtime.registry.call("mcp.fixture.forbidden", {}))["ok"]


async def test_mcp_server_requires_explicit_allowlist():
    with pytest.raises(ValueError, match="allow_tools"):
        async with ExtensionRuntime({"harness": {"mcp_servers": {"x": {"command": "must-not-execute"}}}}):
            pass


def test_remote_schema_references_do_not_trigger_network():
    registry = ToolRegistry()
    async def noop(**args):
        return args
    with pytest.raises(ValueError, match="self-contained"):
        registry.register(Tool("remote", "bad schema", {"$ref": "https://example.com/schema"}, noop))
