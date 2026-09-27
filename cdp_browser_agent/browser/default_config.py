from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_BROWSER_AGENT_CONFIG: dict[str, Any] = {
    "workflows": {"paths": [], "state_dir": "workflow-runs"},
    "model": {
        "provider": "llama.cpp",
        "baseUrl": "http://127.0.0.1:8080/v1",
        "model": "",
        "fallbackModel": "local-model",
        "modelDiscoveryTimeout": 5,
        "apiKey": "",
        "maxTokens": 4096,
        "temperature": 0,
        "apiTimeout": 180,
        "trustEnv": False,
        "enableThinking": False,
        "enableVision": False,
    },
    "browser": {
        "connection": "launch",
        "cdp_url": "http://127.0.0.1:9222",
        "auto_start_cdp": True,
        "executable_path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "remote_debugging_port": 9222,
        "user_data_dir": str(Path.home() / ".browser-agent" / "chrome-profile"),
        "cdp_startup_timeout_ms": 10000,
        "close_auto_started_cdp": False,
        "focus_page": True,
        "start_url": "about:blank",
        "observation_empty_retries": 3,
        "observation_empty_retry_ms": 350,
        "new_page_adoption_timeout_ms": 3000,
        "organize_downloads_by_resource": True,
        "downloads_path": "downloads/browser-agent",
        "headless": False,
        "slow_mo": 0,
        "viewport": "fullscreen",
    },
    "agent": {
        "max_steps": 40,
        "history_compress_after": 40,
        "max_model_errors": 3,
        "strategy_evaluator_enabled": False,
        "write_run_log": True,
        "log_dir": "logs/browser-agent",
        "console_verbose": False,
        "write_download_manifest": True,
        "analyze_downloads_after_run": False,
        "skip_download_model_summary": True,
        "log_observation_summary": True,
        "allow_password_input": False,
        "context_window_tokens": 32768,
        "reserved_output_tokens": 4096,
        "prompt_budget_ratio": 0.85,
        "chars_per_token": 3.0,
        "memory_archive_max_items": 1000,
        "memory_recent_min": 6,
        "memory_recent_max": 40,
        "memory_recall_max_items": 8,
        "memory_summary_chunk_size": 8,
        "memory_summary_max_items": 12,
        "memory_use_model_summaries": False,
        "memory_task_state_enabled": True,
        "memory_action_quality_enabled": True,
        "memory_run_brief_enabled": True,
        "memory_structured_recall_enabled": True,
        "browser_site_memory_enabled": False,
        "browser_site_memory_max_steps": 80,
    },
    "harness": {
        "skill_paths": [],
        "active_skills": [],
        "mcp_servers": {},
        "tool_timeout_seconds": 60,
        "run_timeout_seconds": 600,
        "max_tool_result_chars": 12000,
        "max_skill_chars": 20000,
        "active_skill_budget_chars": 30000,
    },
}


def browser_agent_default_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_BROWSER_AGENT_CONFIG)


def deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_config(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
