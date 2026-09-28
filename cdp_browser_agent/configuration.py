from __future__ import annotations

import json
from pathlib import Path

from .browser.default_config import browser_agent_default_config, deep_merge_config


def load_config(path: str | None = None) -> dict:
    config = browser_agent_default_config()
    if not path:
        return config
    source = Path(path).expanduser().resolve()
    override = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(override, dict):
        raise ValueError("config must contain a JSON object")
    config = deep_merge_config(config, override)
    root = source.parent

    def absolute(value):
        candidate = Path(value).expanduser()
        return str((root / candidate).resolve()) if not candidate.is_absolute() else str(candidate)

    harness = config.setdefault("harness", {})
    harness["skill_paths"] = [absolute(p) for p in harness.get("skill_paths", [])]
    workflows = config.setdefault("workflows", {})
    workflows["paths"] = [absolute(p) for p in workflows.get("paths", [])]
    workflows["state_dir"] = absolute(workflows.get("state_dir", "workflow-runs"))
    web = config.setdefault("web", {})
    web["artifact_dir"] = absolute(web.get("artifact_dir", "downloads/web"))
    crawler = config.setdefault("crawler", {})
    crawler["state_dir"] = absolute(crawler.get("state_dir", "downloads/crawls"))
    processing = config.setdefault("processing", {})
    processing["paths"] = [absolute(p) for p in processing.get("paths", [])]
    processing["replay_paths"] = [absolute(p) for p in processing.get("replay_paths", [])]
    processing["artifact_dir"] = absolute(processing.get("artifact_dir", "downloads/processed"))
    learning = config.setdefault("learning", {})
    learning["replay_paths"] = [absolute(p) for p in learning.get("replay_paths", [])]
    if learning.get("state_dir"):
        learning["state_dir"] = absolute(learning["state_dir"])
    if harness.get("artifact_dir"):
        harness["artifact_dir"] = absolute(harness["artifact_dir"])
    if harness.get("state_dir"):
        harness["state_dir"] = absolute(harness["state_dir"])
    for server in harness.get("mcp_servers", {}).values():
        if server.get("transport", "stdio") == "stdio":
            server["cwd"] = absolute(server.get("cwd", "."))
            command = server.get("command", "")
            if "/" in command or "\\" in command:
                server["command"] = absolute(command)
    for section, keys in {"browser": ["downloads_path", "user_data_dir", "storage_state", "auth_state_path"], "agent": ["log_dir", "memory_dir", "shared_events_path"]}.items():
        for key in keys:
            if config.get(section, {}).get(key):
                config[section][key] = absolute(config[section][key])
    return config
