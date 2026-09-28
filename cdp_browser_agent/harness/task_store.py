"""Durable task snapshots and cross-process human responses, with exclusive run leases."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4


def task_root(config):
    return Path(config.get("harness", {}).get("state_dir") or
                Path(config.get("agent", {}).get("log_dir", "logs/browser-agent")) / "sessions").resolve()


def fingerprint(config):
    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in {"apiKey", "max_steps"}}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    return hashlib.sha256(json.dumps(scrub(config), sort_keys=True, default=str).encode()).hexdigest()


class TaskStore:
    def __init__(self, config):
        self.root = task_root(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "tasks.sqlite3", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, config_hash TEXT, state TEXT, owner TEXT, lease REAL, reply TEXT)")
        self.owner = uuid4().hex
        self.duration = float(config.get("harness", {}).get("run_timeout_seconds", 600)) + 60
        self.config_hash = fingerprint(config)

    def close(self):
        self.db.close()

    def get(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid task ID")
        row = self.db.execute("SELECT state FROM tasks WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown task ID")
        return json.loads(row[0])

    def list(self):
        return [{k: state.get(k) for k in ("run_id", "task", "status", "step", "pending_input", "answer")}
                for row in self.db.execute("SELECT state FROM tasks ORDER BY rowid DESC LIMIT 20")
                for state in [json.loads(row[0])]]

    def acquire(self, state, resume=False):
        run_id = state["run_id"]
        with self.db:
            if not resume:
                self.db.execute("INSERT INTO tasks VALUES(?,?,?,NULL,0,NULL)", (run_id, self.config_hash, json.dumps(state)))
            row = self.db.execute("SELECT config_hash FROM tasks WHERE id=?", (run_id,)).fetchone()
            if not row or row[0] != self.config_hash:
                raise ValueError("Resume requires the same operator configuration")
            updated = self.db.execute("UPDATE tasks SET owner=?,lease=? WHERE id=? AND (owner IS NULL OR lease<?)",
                (self.owner, time.time() + self.duration, run_id, time.time()))
            if updated.rowcount != 1:
                raise ValueError("Task is active; wait for its worker or expired lease")

    def save(self, state):
        with self.db:
            changed = self.db.execute("UPDATE tasks SET state=?,lease=? WHERE id=? AND owner=?",
                (json.dumps(state, ensure_ascii=False), time.time()+self.duration, state["run_id"], self.owner))
            if changed.rowcount != 1:
                raise ValueError("Task lease lost")

    def release(self, run_id):
        with self.db:
            self.db.execute("UPDATE tasks SET owner=NULL,lease=0 WHERE id=? AND owner=?", (run_id, self.owner))

    def respond(self, run_id, request_id, answer):
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 12000:
            raise ValueError("Response must contain 1..12000 characters")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.get(run_id)
            pending = state.get("pending_input") or {}
            if pending.get("id") != request_id or pending.get("deadline", 0) <= time.time():
                raise ValueError("This input request is absent or expired")
            self.db.execute("UPDATE tasks SET reply=? WHERE id=?",
                            (json.dumps({"request_id": request_id, "answer": answer}), run_id))
        return {"run_id": run_id, "accepted": True}

    def consume(self, run_id, request_id):
        with self.db:
            row = self.db.execute("SELECT reply FROM tasks WHERE id=?", (run_id,)).fetchone()
            reply = json.loads(row[0]) if row and row[0] else None
            if reply and reply["request_id"] == request_id:
                self.db.execute("UPDATE tasks SET reply=NULL WHERE id=?", (run_id,))
                return reply["answer"]
