import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cdp_browser_agent.browser import agent
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.harness.session import RunSession
from cdp_browser_agent.harness.intervention import handle_intervention
from cdp_browser_agent.harness.task_store import TaskStore
from cdp_browser_agent.harness.experience import ExperienceStore
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.harness import runtime as runtime_module
from cdp_browser_agent.web import tools as webtools
from cdp_browser_agent.browser.memory import BrowserAgentMemory, estimate_tokens
from cdp_browser_agent.browser.planner import fit_payload


def configuration(tmp_path):
    config = browser_agent_default_config()
    config["agent"].update(log_dir=str(tmp_path / "logs"), max_steps=4)
    config["web"]["artifact_dir"] = str(tmp_path / "web")
    config["model"]["model"] = "fixture"
    return config


def test_repeated_fetch_does_not_count_timestamp_as_progress():
    action = {"action": "tool", "name": "web_fetch", "arguments": {"url": "https://example.com"}}
    a = {"ok": True, "text_sha256": "same", "accessed_at": "one", "artifact_paths": ["first"]}
    b = {**a, "accessed_at": "two", "artifact_paths": ["second"]}
    assert agent.progress_signature({}, action, a) == agent.progress_signature({}, action, b)
    assert agent.progress_signature({}, action, a) != agent.progress_signature({}, action, {**b, "text_sha256": "changed"})


async def test_small_budget_keeps_source_identity(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    config["harness"]["max_tool_result_chars"] = 512
    config["web"]["proxy"] = ""
    response = {"url": "https://example.com", "status_code": 200, "headers": {"content-type": "text/html"},
                "body": b'<article>' + b'Actual content. ' * 60 + b'</article>', "redirects": []}
    monkeypatch.setattr(webtools, "download", AsyncMock(return_value=response))
    runtime = ExtensionRuntime(config)
    result = await runtime.registry.call("web_fetch", {"url": "https://example.com"})
    sources = agent.collect_web_sources([], "web_fetch", result)
    assert result["ok"] and sources[0]["url"] == "https://example.com"
    assert result["artifact_paths"] and result["text_sha256"]


async def test_optional_mcp_failure_isolated_but_required_fails(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    config["harness"]["mcp_servers"] = {"offline": {"command": "fixture", "allow_tools": ["lookup"]}}
    def fail(*args, **kwargs):
        raise ConnectionError("Fixture outage")
    monkeypatch.setattr(runtime_module, "Client", fail)
    async with ExtensionRuntime(config) as runtime:
        assert runtime.context()["unavailable_servers"]["offline"]["error_type"] == "ConnectionError"
        assert runtime.registry.describe("web_fetch")
    config["harness"]["mcp_servers"]["offline"]["required"] = True
    with pytest.raises(ConnectionError):
        async with ExtensionRuntime(config):
            pass


@pytest.mark.parametrize("mode,reason", [("auto", "preauthorized"), ("wait", "wait_timeout")])
async def test_autonomous_intervention_continues(tmp_path, monkeypatch, mode, reason):
    config = configuration(tmp_path)
    config["intervention"].update(mode=mode, wait_seconds=.01)
    planner = AsyncMock(side_effect=[{"action": {"action": "ask_user", "message": "Which source?"}},
                                    {"action": {"action": "done", "outcome": "incomplete", "answer": "Used available evidence; missing input remains explicit."}}])
    monkeypatch.setattr(agent, "plan_next_action", planner)
    result = await run_browser_agent("Read public information", config)
    assert result["status"] == "incomplete" and result["decisions"][0]["reason"] == reason
    assert planner.call_args_list[1].args[0]["last_result"]["intervention"]["mode"] == "auto"


async def test_user_reply_wins_before_wait_timeout(tmp_path):
    config = configuration(tmp_path)
    config["intervention"].update(mode="wait", wait_seconds=2)
    session = RunSession(config, "Read")
    waiter = asyncio.create_task(handle_intervention(session, config, "Choose source"))
    await asyncio.sleep(.03)
    store = TaskStore(config)
    try:
        state = store.get(session.state["run_id"])
        store.respond(state["run_id"], state["pending_input"]["id"], "Use official site")
        await waiter
        assert session.state["decisions"][0]["mode"] == "user"
        with pytest.raises(ValueError, match="expired"):
            store.respond(state["run_id"], state["pending_input"]["id"], "late answer")
    finally:
        store.close()
        session.finish()


async def test_task_resume_restores_plan_and_memory(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    config["intervention"]["mode"] = "return"
    planner = AsyncMock(side_effect=[
        {"action": {"action": "tool", "name": "plan_update", "arguments": {"steps": [{"task": "Read evidence", "status": "in_progress"}]}}},
        {"action": {"action": "ask_user", "message": "Need source"}},
        {"action": {"action": "done", "outcome": "incomplete", "answer": "Retained partial work."}}])
    monkeypatch.setattr(agent, "plan_next_action", planner)
    first = await run_browser_agent("Read", config)
    assert first["status"] == "needs_input" and first["memory_snapshot"]["raw_archive"]
    resumed = await run_browser_agent(None, config, resume_run_id=first["run_id"], user_input="Use partial results")
    assert resumed["run_id"] == first["run_id"] and resumed["attempt"] == 2 and resumed["step"] == 3
    assert resumed["plan"] == first["plan"] and resumed["memory_stats"]["raw_archive"] == 1
    assert planner.call_args.args[0]["memory_context"]["run_notes"]["decisions"][-1]["answer"] == "Use partial results"


def test_experience_requires_two_independent_host_verified_runs(tmp_path):
    store = ExperienceStore(configuration(tmp_path))
    state = {"run_id": "one", "completion_basis": "model_reported", "history": [
        {"action": {"action": "tool", "name": "web_fetch"}, "result": {"ok": False, "status": "insufficient_static_content"}},
        {"action": {"action": "navigate"}, "result": {"ok": True, "url": "https://example.com"}}]}
    try:
        store.record(state)
        assert not store.recall("https://example.com")
        for run_id in ("two", "three"):
            state.update(run_id=run_id, completion_basis="host_verified", verification={"ok": True})
            store.record(state)
        learned = store.recall("https://example.com")
        assert learned[0]["verified_runs"] == 2 and not store.recall("https://other.example")
        store.revoke(learned[0]["memory_id"])
        assert not store.recall("https://example.com")
    finally:
        store.close()


def test_context_preserves_run_notes_and_estimates_chinese():
    notes = {"plan": ["current objective"], "decisions": ["user instruction"]}
    compact = fit_payload({"task": "Read", "run_notes": notes, "recent_history": ["x" * 3000] * 3}, 1000)
    assert compact["run_notes"] == notes
    assert estimate_tokens("中文正文" * 100) > estimate_tokens("abcd" * 100)
    memory = BrowserAgentMemory()
    memory.task_state = {"fact": "keep"}
    resumed = BrowserAgentMemory()
    resumed.restore(memory.snapshot())
    assert resumed.task_state == memory.task_state


def test_busy_resume_does_not_append_to_active_log(tmp_path):
    config = configuration(tmp_path)
    first = RunSession(config, "Read")
    log = first.recorder.path.read_bytes()
    try:
        with pytest.raises(ValueError, match="active"):
            RunSession(config, "Read", first.state["run_id"])
        assert first.recorder.path.read_bytes() == log
    finally:
        first.finish()


async def test_resume_restores_loaded_skills_and_rejects_changed_instructions(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    skill = tmp_path / "skills/reader/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: reader\ndescription: Read sources.\n---\nKeep the exact source.", encoding="utf-8")
    config["harness"]["skill_paths"] = [str(skill.parent.parent)]
    config["intervention"]["mode"] = "return"
    planner = AsyncMock(side_effect=[
        {"action": {"action": "tool", "name": "skill_load", "arguments": {"name": "reader"}}},
        {"action": {"action": "ask_user", "message": "Source?"}},
        {"action": {"action": "ask_user", "message": "Which document?"}}])
    monkeypatch.setattr(agent, "plan_next_action", planner)
    first = await run_browser_agent("Read", config)
    second = await run_browser_agent(None, config, resume_run_id=first["run_id"], user_input="Official source")
    assert second["status"] == "needs_input"
    assert planner.call_args.args[0]["extensions"]["active_skills"][0]["name"] == "reader"
    skill.write_text(skill.read_text() + " Changed instructions.", encoding="utf-8")
    third = await run_browser_agent(None, config, resume_run_id=first["run_id"])
    assert third["status"] == "failed" and "skill changed" in third["answer"]


async def test_browser_storage_state_restored_after_relaunch(tmp_path):
    from cdp_browser_agent.browser.controller import BrowserController
    from examples.workflows.demo_site import demo_server
    config = configuration(tmp_path)
    config["browser"].update(headless=True, focus_page=False)
    with demo_server() as (_, url):
        controller = await BrowserController.launch(config)
        try:
            await controller.page.goto(url + "/catalog?page=1")
            await controller.page.evaluate("localStorage.setItem('fixture-progress', 'remembered')")
            await controller.context.add_cookies([{"name": "fixture_session", "value": "test-cookie", "url": url}])
            saved = await controller.save_session(tmp_path / "browser-state.json")
        finally:
            await controller.close()
        config["browser"]["storage_state"] = saved["browser_state_file"]
        restored = await BrowserController.launch(config)
        try:
            await restored.page.goto(url + "/catalog?page=1")
            assert await restored.page.evaluate("localStorage.getItem('fixture-progress')") == "remembered"
            assert any(c["name"] == "fixture_session" for c in await restored.context.cookies())
        finally:
            await restored.close()
