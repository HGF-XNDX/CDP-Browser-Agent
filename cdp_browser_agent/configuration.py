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
    for server in harness.get("mcp_servers", {}).values():
        if server.get("transport", "stdio") == "stdio":
            server["cwd"] = absolute(server.get("cwd", "."))
            command = server.get("command", "")
            if "/" in command or "\\" in command:
                server["command"] = absolute(command)
    for section, keys in {"browser": ["downloads_path", "user_data_dir"], "agent": ["log_dir", "memory_dir", "shared_events_path"]}.items():
        for key in keys:
            if config.get(section, {}).get(key):
                config[section][key] = absolute(config[section][key])
    return config
