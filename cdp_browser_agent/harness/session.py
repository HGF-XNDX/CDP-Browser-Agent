"""A single owner for run identity, state, and durable terminal events."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class RunLogger:
    def __init__(self, config: dict, task: str):
        settings = config.get("agent", {})
        self.run_id = uuid4().hex
        self.path = None
        self.shared_path = Path(settings["shared_events_path"]).resolve() if settings.get("shared_events_path") else None
        self.workflow_id = settings.get("shared_workflow_id", "")
        if settings.get("write_run_log", True):
            root = Path(settings.get("log_dir", "logs/browser-agent")).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            self.path = root / f"run_{self.run_id}.jsonl"
        if self.shared_path:
            self.shared_path.parent.mkdir(parents=True, exist_ok=True)
        self.write("run_start", {"task": task})

    def write(self, event: str, payload: dict | None = None):
        record = {**(payload or {}), "run_id": self.run_id, "event": event,
                  "ts": datetime.now(timezone.utc).isoformat()}
        if self.path:
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.shared_path:
            with self.shared_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps({**record, "workflow_id": self.workflow_id, "actor": "cdp_agent",
                                      "kind": f"cdp_{event}"}, ensure_ascii=False, default=str) + "\n")


class RunSession:
    def __init__(self, config: dict, task: str):
        self.recorder = RunLogger(config, task)
        self.state = {"run_id": self.recorder.run_id, "task": task, "status": "running",
                      "step": 0, "answer": "", "history": [], "sources": [],
                      "collected_files": [], "last_result": None, "observed_resource_candidates": [],
                      "log_file": str(self.recorder.path) if self.recorder.path else None}
        self.finished = False

    def finish(self):
        if not self.finished:
            self.recorder.write("run_end", {key: self.state.get(key) for key in
                ("status", "stopped_reason", "step", "answer", "collected_files", "completion_basis")})
            self.finished = True
