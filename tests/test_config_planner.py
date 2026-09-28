import json
from pathlib import Path

import pytest

from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.browser.planner import fit_payload


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
