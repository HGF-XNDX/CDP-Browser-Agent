import json
from pathlib import Path

import pytest

from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.browser.planner import decode_planner_action, fit_payload


def test_config_paths_are_relative_to_config_file(tmp_path, monkeypatch):
    file = tmp_path / "config.json"
    file.write_text(json.dumps({"harness": {"skill_paths": ["skills"], "mcp_servers": {"example": {"command": "./python.exe", "allow_tools": []}}}}))
    monkeypatch.chdir(Path(tmp_path).parent)
    config = load_config(str(file))
    assert config["harness"]["skill_paths"] == [str(tmp_path / "skills")]
    assert config["crawler"]["state_dir"] == str(tmp_path / "downloads" / "crawls")
    assert config["harness"]["mcp_servers"]["example"]["command"] == str(tmp_path / "python.exe")


def test_budget_preserves_task_and_active_skills():
    payload = {"task": "Extract", "extensions": {"active_skills": ["important"]}, "recent_history": ["x" * 20000], "observation": {"fullText": "y" * 9000}}
    small = fit_payload(payload, 3000)
    assert len(json.dumps(small, ensure_ascii=False)) <= 3000
    assert small["task"] == payload["task"]
    assert small["extensions"] == payload["extensions"]
    assert small["context_truncated_fields"]
    assert payload["recent_history"]
    with pytest.raises(ValueError, match="exceeds prompt budget"):
        fit_payload({"task": "X" * 9000}, 1000)


def test_single_action_protocol_retains_first_complete_object_and_audits_later_actions():
    first = {"action": "tool", "name": "document_inspect", "arguments": {
        "source_id": "source", "selector": 'p[data-label="{unit}"]'}}
    raw = json.dumps(first) + '\nDBG\n' + json.dumps({"action": "done", "outcome": "completed", "answer": "invented result"})
    selected, receipt = decode_planner_action(raw)
    assert selected == first
    assert receipt['mode'] == 'first_object' and receipt['ignored_suffix_chars'] > 0
    # Nested braces and quoted selector text must not truncate the real action.
    assert selected['arguments']['selector'] == 'p[data-label="{unit}"]'


def test_single_action_protocol_does_not_repair_broken_json_or_skip_to_later_action():
    raw = '{"action":"tool","arguments":{"value":broken}}\n{"action":"done"}'
    with pytest.raises(json.JSONDecodeError):
        decode_planner_action(raw)
    with pytest.raises(ValueError, match='first JSON object'):
        decode_planner_action('{}\n{"action":"done"}')
