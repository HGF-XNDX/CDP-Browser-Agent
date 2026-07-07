from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_REUSABLE_ACTIONS = {
    "click",
    "type",
    "press",
    "navigate",
    "download",
    "save_page",
}


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _path_pattern(url: str) -> str:
    try:
        path = urlparse(url or "").path or "/"
    except Exception:
        return "/"
    parts = [part for part in path.split("/") if part][:3]
    return "/" + "/".join(parts) if parts else "/"


def _compact(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _reusable_action_url(action: dict) -> str:
    """Keep only stable navigation URLs; resource/detail/download URLs are dynamic."""
    if action.get("action") not in {"navigate", "open_tab"}:
        return ""
    value = str(action.get("url") or "")
    try:
        parsed = urlparse(value)
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc or parsed.query or parsed.fragment:
        return ""
    return _compact(value, 700)


def _target_fingerprint(element: dict | None) -> dict:
    element = element or {}
    return {
        "tag": _compact(element.get("tag"), 40),
        "role": _compact(element.get("role"), 40),
        "type": _compact(element.get("type"), 40),
        "text": _compact(element.get("text"), 120),
        "placeholder": _compact(element.get("placeholder"), 120),
        "aria_label": _compact(element.get("ariaLabel"), 120),
        "name": _compact(element.get("name"), 100),
        "description": _compact(element.get("description"), 220),
        "nearby_text": _compact(element.get("nearbyText"), 260),
    }


def target_element(observation: dict, action: dict) -> dict:
    target_id = str(action.get("target_id") or "")
    if not target_id:
        return {}
    for element in observation.get("elements") or []:
        if str(element.get("id") or "") == target_id:
            return element
    return {}


class BrowserSiteMemory:
    """Cross-run memory for stable site interaction knowledge."""

    def __init__(self, path: Path, max_steps_per_site: int = 80) -> None:
        self.path = path.resolve()
        self.max_steps_per_site = max(10, int(max_steps_per_site))
        self.data: dict = {"version": 1, "sites": {}}
        self.load()

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("sites"), dict):
                self.data = value
                return True
        except Exception:
            pass
        return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def record(self, entry: dict) -> None:
        action = entry.get("action") or {}
        action_name = str(action.get("action") or "")
        if action_name not in _REUSABLE_ACTIONS:
            return
        url = str(entry.get("url") or "")
        hostname = _host(url)
        if not hostname:
            return
        result = entry.get("result") or {}
        success = result.get("ok") is True
        site = self.data.setdefault("sites", {}).setdefault(
            hostname,
            {
                "host": hostname,
                "successful_steps": [],
                "failed_steps": [],
                "successful_workflows": [],
            },
        )
        step = {
            "path": _path_pattern(url),
            "page_title": _compact(entry.get("title"), 160),
            "page_type": _compact(entry.get("pageType"), 80),
            "action": action_name,
            "target": _target_fingerprint(entry.get("targetElement")),
            "key": _compact(action.get("key"), 40),
            "url": _reusable_action_url(action),
            "filename_pattern": _compact(action.get("filename"), 180),
            "reason": _compact(action.get("reason"), 260),
            "result": _compact(result.get("message"), 260),
            "last_seen": datetime.now().isoformat(timespec="seconds"),
        }
        collection = site["successful_steps"] if success else site["failed_steps"]
        signature = json.dumps(
            {
                "path": step["path"],
                "action": step["action"],
                "target": step["target"],
                "key": step["key"],
                "url": step["url"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for existing in collection:
            if existing.get("_signature") == signature:
                existing.update(step)
                existing["uses"] = int(existing.get("uses") or 1) + 1
                break
        else:
            collection.append({**step, "_signature": signature, "uses": 1})
        collection[:] = collection[-self.max_steps_per_site :]

    def record_successful_workflow(self, task: str, entries: list[dict]) -> None:
        grouped: dict[str, list[dict]] = {}
        for entry in entries:
            result = entry.get("result") or {}
            action = entry.get("action") or {}
            hostname = _host(str(entry.get("url") or ""))
            if (
                hostname
                and result.get("ok") is True
                and action.get("action") in _REUSABLE_ACTIONS
            ):
                grouped.setdefault(hostname, []).append(entry)
        for hostname, host_entries in grouped.items():
            site = self.data.setdefault("sites", {}).setdefault(
                hostname,
                {
                    "host": hostname,
                    "successful_steps": [],
                    "failed_steps": [],
                    "successful_workflows": [],
                },
            )
            workflow_steps = []
            for entry in host_entries[-12:]:
                action = entry.get("action") or {}
                workflow_steps.append(
                    {
                        "path": _path_pattern(str(entry.get("url") or "")),
                        "page_type": _compact(entry.get("pageType"), 80),
                        "action": action.get("action", ""),
                        "target": _target_fingerprint(entry.get("targetElement")),
                        "key": _compact(action.get("key"), 40),
                        "url": _reusable_action_url(action),
                        "reason": _compact(action.get("reason"), 220),
                    }
                )
            workflow = {
                "task_pattern": _compact(task, 280),
                "steps": workflow_steps,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "uses": 1,
            }
            workflows = site["successful_workflows"]
            signature = json.dumps(workflow_steps, ensure_ascii=False, sort_keys=True)
            for existing in workflows:
                if existing.get("_signature") == signature:
                    existing.update(workflow)
                    existing["_signature"] = signature
                    existing["uses"] = int(existing.get("uses") or 1) + 1
                    break
            else:
                workflows.append({**workflow, "_signature": signature})
            workflows[:] = workflows[-12:]

    def recall(self, url: str) -> dict:
        hostname = _host(url)
        site = (self.data.get("sites") or {}).get(hostname)
        if not site:
            return {}
        current_path = _path_pattern(url)
        successful = sorted(
            site.get("successful_steps") or [],
            key=lambda item: (
                item.get("path") == current_path,
                int(item.get("uses") or 0),
                item.get("last_seen") or "",
            ),
            reverse=True,
        )
        failed = sorted(
            site.get("failed_steps") or [],
            key=lambda item: (
                item.get("path") == current_path,
                int(item.get("uses") or 0),
            ),
            reverse=True,
        )
        workflows = sorted(
            site.get("successful_workflows") or [],
            key=lambda item: (
                int(item.get("uses") or 0),
                item.get("completed_at") or "",
            ),
            reverse=True,
        )
        return {
            "host": hostname,
            "current_path": current_path,
            "instruction": (
                "Reuse semantic control fingerprints against the CURRENT observation. "
                "Never reuse an old target_id blindly. Prefer a proven workflow when "
                "the current page structure matches; fall back to exploration if it does not."
            ),
            "successful_workflows": [
                {k: v for k, v in item.items() if k != "_signature"}
                for item in workflows[:3]
            ],
            "proven_steps": [
                {k: v for k, v in item.items() if k != "_signature"}
                for item in successful[:12]
            ],
            "known_failures": [
                {k: v for k, v in item.items() if k != "_signature"}
                for item in failed[:6]
            ],
        }

    def stats(self) -> dict:
        sites = self.data.get("sites") or {}
        return {
            "path": str(self.path),
            "sites": len(sites),
            "successful_steps": sum(
                len(site.get("successful_steps") or []) for site in sites.values()
            ),
            "successful_workflows": sum(
                len(site.get("successful_workflows") or []) for site in sites.values()
            ),
        }
