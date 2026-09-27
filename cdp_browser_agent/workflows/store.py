"""Transactional page checkpoints and immutable per-run observations in SQLite."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from .spec import digest


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data):
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class WorkflowBusy(RuntimeError):
    pass


class WorkflowStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "workflows.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, name TEXT NOT NULL, spec_hash TEXT NOT NULL,
                params_hash TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
                status TEXT NOT NULL, state TEXT NOT NULL, owner TEXT,
                lease_until REAL NOT NULL DEFAULT 0, pause_requested INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS records (
                run_id TEXT NOT NULL, step_id TEXT NOT NULL, record_key TEXT NOT NULL,
                content_hash TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (run_id, step_id, record_key));
            CREATE TABLE IF NOT EXISTS pages (
                run_id TEXT NOT NULL, step_id TEXT NOT NULL, url TEXT NOT NULL,
                evidence TEXT NOT NULL, PRIMARY KEY (run_id, step_id, url));
        """)
        self.owner = uuid4().hex
        self.lease_seconds = 660

    def close(self):
        self.db.close()

    def get(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid workflow run ID")
        row = self.db.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown workflow run ID")
        return json.loads(row["state"])

    def create(self, spec, concrete, parameters):
        spec_hash, params_hash = digest(spec), digest(parameters)
        baseline = self.db.execute("SELECT run_id FROM runs WHERE name=? AND spec_hash=? AND params_hash=? AND status='completed' ORDER BY updated DESC LIMIT 1",
                                   (spec["name"], spec_hash, params_hash)).fetchone()
        run_id = uuid4().hex
        state = {"run_id": run_id, "workflow": spec["name"], "version": spec["version"], "spec_hash": spec_hash,
                 "spec": concrete, "parameters": parameters, "baseline_run_id": baseline[0] if baseline else None,
                 "status": "pending", "step_index": 0, "cursors": {}, "step_results": {}, "attempt": 0,
                 "created_at": now(), "updated_at": now(), "completion_basis": "unverified"}
        with self.db:
            self.db.execute("INSERT INTO runs(run_id,name,spec_hash,params_hash,created,updated,status,state) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, spec["name"], spec_hash, params_hash, state["created_at"], state["updated_at"], state["status"], json.dumps(state)))
        return state

    def acquire(self, run_id, lease_seconds):
        self.lease_seconds = lease_seconds
        with self.db:
            cursor = self.db.execute("UPDATE runs SET owner=?, lease_until=?, pause_requested=0 WHERE run_id=? AND (owner IS NULL OR lease_until<?)",
                                     (self.owner, time.time() + lease_seconds, run_id, time.time()))
            if cursor.rowcount != 1:
                raise WorkflowBusy("Workflow run is already active; retry after its lease expires if its worker crashed")

    def release(self, run_id):
        with self.db:
            self.db.execute("UPDATE runs SET owner=NULL,lease_until=0 WHERE run_id=? AND owner=?", (run_id, self.owner))

    def _save(self, state):
        state["updated_at"] = now()
        cursor = self.db.execute("UPDATE runs SET state=?,status=?,updated=?,lease_until=? WHERE run_id=? AND owner=?",
            (json.dumps(state, ensure_ascii=False), state["status"], state["updated_at"], time.time() + self.lease_seconds, state["run_id"], self.owner))
        if cursor.rowcount != 1:
            raise WorkflowBusy("Workflow lease was lost")

    def save(self, state):
        with self.db:
            self._save(state)

    def request_pause(self, run_id):
        state = self.get(run_id)
        with self.db:
            self.db.execute("UPDATE runs SET pause_requested=1 WHERE run_id=?", (run_id,))
        return {"run_id": run_id, "status": state["status"], "pause_requested": True}

    def pause_requested(self, run_id):
        return bool(self.db.execute("SELECT pause_requested FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])

    def seen_page(self, run_id, step_id, url):
        return self.db.execute("SELECT 1 FROM pages WHERE run_id=? AND step_id=? AND url=?", (run_id, step_id, url)).fetchone() is not None

    def count(self, run_id, step_id=None):
        query = "SELECT COUNT(*) FROM records WHERE run_id=?"
        args = (run_id,)
        if step_id:
            query += " AND step_id=?"
            args += (step_id,)
        return self.db.execute(query, args).fetchone()[0]

    def commit_page(self, state, step, url, records, evidence, cursor):
        updated = deepcopy(state)
        updated["cursors"][step["id"]] = cursor
        with self.db:
            # Cursor, records and page evidence either all commit or all roll back.
            for record in records:
                key = digest([record["data"].get(name) for name in step["key_fields"]])
                content_hash = digest(record["data"])
                existing = self.db.execute("SELECT content_hash FROM records WHERE run_id=? AND step_id=? AND record_key=?",
                    (state["run_id"], step["id"], key)).fetchone()
                if existing and existing[0] != content_hash:
                    raise ValueError("Conflicting data for the same key within a run; choose stable unique key_fields")
                baseline = self.db.execute("SELECT content_hash FROM records WHERE run_id=? AND step_id=? AND record_key=?",
                    (state["baseline_run_id"], step["id"], key)).fetchone() if state["baseline_run_id"] else None
                change = "new" if baseline is None else ("unchanged" if baseline[0] == content_hash else "changed")
                payload = {**record, "step_id": step["id"], "record_key": key, "content_hash": content_hash,
                           "captured_at": now(), "change": change}
                self.db.execute("INSERT OR IGNORE INTO records VALUES(?,?,?,?,?)",
                    (state["run_id"], step["id"], key, content_hash, json.dumps(payload, ensure_ascii=False)))
            self.db.execute("INSERT INTO pages VALUES(?,?,?,?)", (state["run_id"], step["id"], url, json.dumps(evidence, ensure_ascii=False)))
            self._save(updated)
        state.clear()
        state.update(updated)

    def records(self, run_id):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM records WHERE run_id=? ORDER BY rowid", (run_id,))]

    def result(self, state):
        records = self.records(state["run_id"])
        return {key: state[key] for key in ("run_id", "workflow", "version", "status", "step_index", "attempt", "completion_basis", "baseline_run_id")} | {
            "record_count": len(records), "changes": {kind: sum(r["change"] == kind for r in records) for kind in ("new", "changed", "unchanged")},
            "step_results": state["step_results"], "error": state.get("error"),
            "output_dir": str(self.root / state["run_id"]), "verification": state.get("verification")}

    def export(self, state):
        directory = self.root / state["run_id"]
        directory.mkdir(exist_ok=True)
        records = self.records(state["run_id"])
        for filename, selected in (("records.jsonl", records), ("changes.jsonl", [r for r in records if r["change"] != "unchanged"])):
            temp = directory / (filename + "." + uuid4().hex + ".tmp")
            temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected), encoding="utf-8")
            temp.replace(directory / filename)
        pages = [json.loads(r[0]) for r in self.db.execute("SELECT evidence FROM pages WHERE run_id=? ORDER BY rowid", (state["run_id"],))]
        atomic_json(directory / "pages.json", pages)
        atomic_json(directory / "workflow.json", state["spec"])
        result = self.result(state)
        atomic_json(directory / "result.json", result)
        return result
