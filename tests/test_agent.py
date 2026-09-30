import asyncio
import json
import sys
from pathlib import Path
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from cdp_browser_agent.browser import agent
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.browser.policy import validate_action


@pytest.fixture
def setup_run(monkeypatch, tmp_path):
    config = browser_agent_default_config()
    config["intervention"]["mode"] = "return"
    config["web"]["prefer_fast_path"] = False  # Existing browser-lifecycle contracts.
    config["agent"].update(max_steps=4, log_dir=str(tmp_path / "logs"), write_download_manifest=False)
    controller = AsyncMock()
    controller.observe.return_value = {"url": "https://example.com", "title": "Example", "visibleText": "A useful page", "elements": []}
    controller.page_context.return_value = {"pages": []}
    controller.execute.return_value = {"ok": True, "message": "waited"}
    monkeypatch.setattr(agent.BrowserController, "launch", AsyncMock(return_value=controller))
    return config, controller


async def test_download_is_not_whole_task_completion(setup_run, monkeypatch, tmp_path):
    config, controller = setup_run
    file = tmp_path / "page.txt"
    file.write_text("content")
    controller.execute.return_value = {"ok": True, "path": str(file), "message": "saved_page"}
    actions = [{"action": {"action": "save_page"}}, {"action": {"action": "done", "outcome": "completed", "answer": "Compared both pages."}}]
    monkeypatch.setattr(agent, "plan_next_action", AsyncMock(side_effect=actions))
    original = deepcopy(config)
    result = await run_browser_agent("Save and compare", config)
    assert result["status"] == "completed", result
    assert result["step"] == 2
    assert result["collected_files"] == [str(file)]
    assert config == original
    controller.close.assert_awaited_once()
    events = [json.loads(line) for line in open(result["log_file"], encoding="utf-8")]
    assert events[-1]["status"] == "completed"


@pytest.mark.parametrize("action,status", [({"action": "ask_user", "message": "Login needed"}, "needs_input"), ({"action": "wait", "ms": 0}, "max_steps")])
async def test_non_success_outcomes_are_explicit(setup_run, monkeypatch, action, status):
    config, controller = setup_run
    monkeypatch.setattr(agent, "plan_next_action", AsyncMock(return_value={"action": action}))
    result = await run_browser_agent("Inspect page", config)
    assert result["status"] == status, result
    controller.close.assert_awaited_once()


async def test_timeout_closes_browser(setup_run, monkeypatch):
    config, controller = setup_run
    config["harness"]["run_timeout_seconds"] = .1
    async def slow(_):
        await asyncio.sleep(100)
    monkeypatch.setattr(agent, "plan_next_action", slow)
    result = await run_browser_agent("Inspect", config)
    assert result["status"] == "timeout"
    assert result["log_file"]
    controller.close.assert_awaited_once()
    events = [json.loads(line) for line in Path(result["log_file"]).read_text(encoding="utf-8").splitlines()]
    assert [e["status"] for e in events if e["event"] == "run_end"] == ["timeout"]


@pytest.mark.parametrize("outcome", ["incomplete", "blocked"])
async def test_done_can_report_non_completion(setup_run, monkeypatch, outcome):
    config, _ = setup_run
    monkeypatch.setattr(agent, "plan_next_action", AsyncMock(return_value={"action": {
        "action": "done", "outcome": outcome, "answer": "Could not complete the requested change."}}))
    result = await run_browser_agent("Change the setting", config)
    assert result["status"] == outcome
    assert result["completion_basis"] == "model_reported"


async def test_extension_startup_failure_has_terminal_log(setup_run):
    config, controller = setup_run
    config["harness"]["mcp_servers"] = {"missing_allowlist": {"command": "unused"}}
    result = await run_browser_agent("Inspect", config)
    assert result["status"] == "failed" and result["run_id"]
    events = [json.loads(line) for line in Path(result["log_file"]).read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "run_start"
    assert [e["status"] for e in events if e["event"] == "run_end"] == ["failed"]
    controller.close.assert_not_awaited()


async def test_external_cancellation_propagates_and_logs(setup_run, monkeypatch):
    config, controller = setup_run
    entered = asyncio.Event()
    async def slow(_):
        entered.set()
        await asyncio.sleep(100)
    monkeypatch.setattr(agent, "plan_next_action", slow)
    task = asyncio.create_task(run_browser_agent("Inspect", config))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = [json.loads(line) for line in next(Path(config["agent"]["log_dir"]).glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert [e["status"] for e in events if e["event"] == "run_end"] == ["cancelled"]
    controller.close.assert_awaited_once()


def test_done_requires_explicit_outcome_and_disabled_vision_is_rejected():
    with pytest.raises(Exception, match="outcome"):
        validate_action({"action": "done", "answer": "Unable to finish"}, {})
    with pytest.raises(ValueError, match="Vision is disabled"):
        validate_action({"action": "observe_vision"}, {})


async def test_clicked_download_is_in_collected_files(setup_run, monkeypatch, tmp_path):
    config, controller = setup_run
    path = tmp_path / "download.csv"
    path.write_text("sku,quantity\nA17,8\n")
    controller.observe.return_value["elements"] = [{"id": "link_1", "tag": "a"}]
    controller.execute.return_value = {"ok": True, "message": "downloaded", "path": str(path)}
    actions = [{"action": {"action": "click", "target_id": "link_1"}},
               {"action": {"action": "done", "outcome": "completed", "answer": "Saved the CSV."}}]
    monkeypatch.setattr(agent, "plan_next_action", AsyncMock(side_effect=actions))
    result = await run_browser_agent("Download report", config)
    assert result["collected_files"] == [str(path)]


async def test_model_failure_is_bounded(setup_run, monkeypatch):
    config, controller = setup_run
    monkeypatch.setattr(agent, "plan_next_action", AsyncMock(side_effect=ValueError("bad JSON")))
    result = await run_browser_agent("Inspect", config)
    assert result["status"] == "failed"
    assert result["step"] == 3


def test_progress_detects_same_page_changes():
    a = {"url": "https://example.com", "visibleText": "before"}
    b = {**a, "visibleText": "after"}
    assert agent.progress_signature(a, {}, {}) != agent.progress_signature(b, {}, {})
    assert agent.progress_signature(a, {}, {}) == agent.progress_signature(a, {}, {})


async def test_repeated_tool_result_gets_recovery_feedback_before_stall(setup_run, monkeypatch):
    config, _ = setup_run
    config['web']['prefer_fast_path'] = True
    calls = []
    async def planner(request):
        calls.append(deepcopy(request['extensions'].get('progress_recovery')))
        if len(calls) <= 2:
            return {'action': {'action': 'tool', 'name': 'tool_list', 'arguments': {'query': 'no-such-capability'}}}
        recovery = request['extensions']['progress_recovery']
        assert recovery['repeat_count'] == 2
        assert recovery['evidence_action_ids'] == ['A0001', 'A0002']
        return {'action': {'action': 'done', 'outcome': 'incomplete', 'answer': 'Required capability is absent.'}}
    monkeypatch.setattr(agent, 'plan_next_action', planner)
    result = await run_browser_agent('Inspect missing capability', config)
    assert result['status'] == 'incomplete'
    assert calls[:2] == [None, None]


async def test_host_verifier_rejects_premature_done(setup_run, monkeypatch):
    config, _ = setup_run
    planner = AsyncMock(return_value={"action": {"action": "done", "outcome": "completed", "answer": "Finished"}})
    verifier = AsyncMock(side_effect=[{"ok": False, "missing": "Required page state"}, {"ok": True, "evidence": "Page state verified"}])
    monkeypatch.setattr(agent, "plan_next_action", planner)
    result = await run_browser_agent("Perform a task", config, completion_check=verifier)
    assert result["status"] == "completed" and result["step"] == 2
    assert result["completion_basis"] == "host_verified"
    assert verifier.await_count == 2
    assert planner.call_args_list[1].args[0]["last_result"]["errorType"] == "completion_check_failed"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://user:secret@example.com"])
def test_invalid_navigation_is_rejected(url):
    with pytest.raises(ValueError):
        validate_action({"action": "navigate", "url": url}, {})


def test_stale_elements_and_passwords():
    with pytest.raises(ValueError):
        validate_action({"action": "click", "target_id": "missing"}, {"elements": []})
    result = validate_action({"action": "type", "target_id": "p", "text": "secret"}, {"elements": [{"id": "p", "type": "password"}]})
    assert result["action"] == "ask_user"


def test_file_names_and_collision_preservation(tmp_path):
    from cdp_browser_agent.browser.controller import safe_artifact_name, artifact_target
    assert safe_artifact_name("../") == "downloaded_resource"
    assert safe_artifact_name("NUL.txt") == "file_NUL.txt"
    old = tmp_path / "report.txt"
    old.write_text("preserve")
    new = artifact_target(tmp_path, "report.txt")
    assert new != old and new.parent == tmp_path
    assert old.read_text() == "preserve"


async def test_deadline_with_connected_external_mcp(setup_run, monkeypatch):
    config, controller = setup_run
    config["harness"].update(run_timeout_seconds=2, mcp_servers={"fixture": {
        "command": sys.executable,
        "args": [str(Path(__file__).parent / "fixtures" / "external_mcp.py")],
        "allow_tools": ["add"]}})
    async def slow(_):
        await asyncio.sleep(100)
    monkeypatch.setattr(agent, "plan_next_action", slow)
    result = await run_browser_agent("Inspect", config)
    assert result["status"] == "timeout", result
    assert result["log_file"]
    controller.close.assert_awaited_once()
