"""Versioned procedural memory. Model proposals never activate themselves.

SQLite owns revisions, evidence, leases and adoption; prompts are only projections.
This is an ACE-inspired implementation, not a vendored copy of the research code.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from jsonschema import Draft202012Validator

from .task_store import task_root
from ..workflows.spec import digest


LESSON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["trigger", "guidance", "avoid", "evidence_ids"],
    "properties": {
        **{key: {"type": "string", "minLength": 1, "maxLength": size}
           for key, size in (("trigger", 500), ("guidance", 1600), ("avoid", 500))},
        "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 12,
                         "uniqueItems": True, "items": {"type": "string", "maxLength": 100}},
    },
}


def settings(config):
    value = {"enabled": False, "auto_replay": True, "reflect_success": False,
             "task_type": "general", "replay_paths": [], "ttl_days": 30,
             "max_entries": 4, "max_context_chars": 6000, "timeout_seconds": 180,
             "max_candidates": 2, **config.get("learning", {})}
    for key, lo, hi in (("ttl_days", 1, 365), ("max_entries", 1, 12),
                        ("max_context_chars", 500, 20000), ("timeout_seconds", 10, 600),
                        ("max_candidates", 1, 2)):
        if type(value[key]) is not int or not lo <= value[key] <= hi:
            raise ValueError(f"learning.{key} must be an integer in {lo}..{hi}")
    for key in ("enabled", "auto_replay", "reflect_success"):
        if type(value[key]) is not bool:
            raise ValueError(f"learning.{key} must be boolean")
    if not isinstance(value["task_type"], str) or not re.fullmatch(r"[\w-]{1,64}", value["task_type"]):
        raise ValueError("learning.task_type must be a short operator-defined name")
    if not isinstance(value["replay_paths"], list) or not all(isinstance(p, str) and p for p in value["replay_paths"]):
        raise ValueError("learning.replay_paths must be a list of paths")
    return value


def host_of(url):
    try:
        parts = urlsplit(url or "")
        return parts.netloc.lower() if parts.scheme in {"http", "https"} and not parts.username else ""
    except ValueError:
        return ""


def scope(config, target, method_hash, host="", profile=""):
    return {"target": target, "method_hash": method_hash, "host": host,
            "profile": profile, "task_type": settings(config)["task_type"]}


def model_identity(model):
    # Credentials, discovery caches and logging paths do not define a method.
    return {k: v for k, v in model.items() if k not in {"apiKey", "apiKeyEnv", "allowPasswordInput"} and not k.startswith("_")}


def planner_method(config, runtime, model):
    from ..browser.planner import SYSTEM_PROMPT
    from .. import __version__
    return digest({"version": __version__, "system": SYSTEM_PROMPT, "model": model_identity(model),
                   "tools": [runtime.registry.describe(n) for n in sorted(runtime.registry._tools)],
                   "skills": runtime.skills.active,
                   "password_input": config.get("agent", {}).get("allow_password_input", False)})


def advice(entry):
    return {"id": entry["id"], "version": entry["version"], **entry["lesson"],
            "basis": "paired_replay", "scope": entry["scope"]}


class PlaybookStore:
    def __init__(self, config):
        self.settings = settings(config)
        self.root = Path(self.settings.get("state_dir") or task_root(config) / "playbook").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "playbook.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS versions (
            id TEXT, version INTEGER, state TEXT, scope_key TEXT, body TEXT,
            created REAL, expires REAL, PRIMARY KEY(id,version));
          CREATE UNIQUE INDEX IF NOT EXISTS one_active ON versions(id) WHERE state='active';
          CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, version INTEGER, kind TEXT, data TEXT, created REAL);
          CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, body TEXT);
          CREATE TABLE IF NOT EXISTS origins (id TEXT, source_id TEXT, PRIMARY KEY(id,source_id));
          CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, lease REAL, result TEXT);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def event(self, entry, kind, data):
        self.db.execute("INSERT INTO events(id,version,kind,data,created) VALUES(?,?,?,?,?)",
                        (entry["id"], entry["version"], kind, json.dumps(data, ensure_ascii=False), time.time()))

    @staticmethod
    def unpack(row):
        return {**json.loads(row["body"]), **{k: row[k] for k in ("id", "version", "state", "created", "expires")}}

    def get(self, identity, version=None):
        row = self.db.execute("SELECT * FROM versions WHERE id=? AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1",
                              (identity, version, version)).fetchone()
        if not row:
            raise ValueError("Unknown playbook entry/version")
        return self.unpack(row)

    def list(self, *, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid playbook pagination")
        rows = self.db.execute("SELECT * FROM versions ORDER BY created DESC,id,version DESC LIMIT ? OFFSET ?", (limit+1, offset)).fetchall()
        return {"entries": [self.unpack(r) for r in rows[:limit]],
                "next_offset": offset+limit if len(rows) > limit else None}

    def history(self, identity):
        self.get(identity)
        return [dict(r) | {"data": json.loads(r["data"])} for r in self.db.execute(
            "SELECT * FROM events WHERE id=? ORDER BY seq", (identity,))]

    def save_evidence(self, evidence):
        identity = digest(evidence)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?)", (identity, json.dumps(evidence, ensure_ascii=False)))
        return identity

    def read_evidence(self, identity):
        row = self.db.execute("SELECT body FROM evidence WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Missing learning evidence")
        return json.loads(row[0])

    def propose(self, lesson, source_id, *, identity=None, expected_version=None):
        Draft202012Validator(LESSON_SCHEMA).validate(lesson)
        source = self.read_evidence(source_id)
        known = {e["id"] for e in source["events"]}
        if not set(lesson["evidence_ids"]) <= known:
            raise ValueError("Lesson cites nonexistent evidence")
        # Exact semantic text duplicates share identity; IDs and source references are not content.
        content = {k: " ".join(lesson[k].split()) for k in ("trigger", "guidance", "avoid")}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if identity:
                old = self.get(identity)
                if old["version"] != expected_version or old["scope"] != source["scope"] or old["state"] == "retired":
                    raise ValueError("Stale revision, retired entry or mismatched scope")
                version = old["version"] + 1
            else:
                identity = digest({"scope": source["scope"], "content": content})[:32]
                row = self.db.execute("SELECT 1 FROM versions WHERE id=?", (identity,)).fetchone()
                if row:
                    self.db.execute("INSERT OR IGNORE INTO origins VALUES(?,?)", (identity, source_id))
                    self.event(self.get(identity), "corroborated", {"source_id": source_id})
                    previous = self.get(identity)
                    if previous["expires"] > time.time() or previous["state"] == "retired":
                        return previous
                    version = previous["version"] + 1
                else:
                    version = 1
            body = {"scope": source["scope"], "lesson": lesson, "source_id": source_id,
                    "origin_hashes": source["origin_hashes"]}
            now = time.time()
            self.db.execute("INSERT INTO versions VALUES(?,?,'candidate',?,?,?,?)",
                            (identity, version, digest(source["scope"]), json.dumps(body, ensure_ascii=False),
                             now, now+self.settings["ttl_days"]*86400))
            self.db.execute("INSERT OR IGNORE INTO origins VALUES(?,?)", (identity, source_id))
            entry = self.get(identity, version)
            self.event(entry, "proposed", {"source_id": source_id})
        return entry

    def origin_hashes(self, identity):
        return {h for row in self.db.execute("SELECT source_id FROM origins WHERE id=?", (identity,))
                for h in self.read_evidence(row[0])["origin_hashes"]}

    def context_revision(self, selected_scope):
        return digest([list(r) for r in self.db.execute(
            "SELECT id,version FROM versions WHERE state='active' AND scope_key=? AND expires>? ORDER BY id,version",
            (digest(selected_scope), time.time()))])

    def recall(self, selected_scope, query=""):
        rows = self.db.execute("SELECT * FROM versions WHERE state='active' AND scope_key=? AND expires>? ORDER BY created DESC",
                               (digest(selected_scope), time.time())).fetchall()
        entries = [self.unpack(r) for r in rows]
        terms = set(re.findall(r"\w+", query.lower()))
        entries.sort(key=lambda e: len(terms & set(re.findall(r"\w+", json.dumps(e["lesson"], ensure_ascii=False).lower()))), reverse=True)
        output, size = [], 2
        for entry in entries:
            value = advice(entry)
            length = len(json.dumps(value, ensure_ascii=False)) + 2
            if size + length <= self.settings["max_context_chars"]:
                output.append(value)
                size += length
            if len(output) >= self.settings["max_entries"]:
                break
        return output

    def validate_advice(self, values):
        for value in values:
            entry = self.get(value["id"], value["version"])
            if entry["state"] != "active" or entry["expires"] <= time.time():
                raise ValueError("Frozen context used inactive/expired playbook advice; start a fresh turn")

    def retire(self, identity, expected_version):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            entry = self.get(identity)
            if entry["version"] != expected_version:
                raise ValueError("Stale playbook revision")
            self.db.execute("UPDATE versions SET state='retired' WHERE id=?", (identity,))
            self.event(entry, "retired", {})
        return self.get(identity)

    def rollback(self, identity, expected_version, restore_version):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            head, restore = self.get(identity), self.get(identity, restore_version)
            if head["version"] != expected_version or restore_version >= expected_version:
                raise ValueError("Stale/invalid rollback revision")
            if restore["state"] != "superseded" or restore["expires"] <= time.time() or head["state"] == "retired":
                raise ValueError("Rollback needs a previously validated, unexpired version")
            self.db.execute("UPDATE versions SET state='rolled_back' WHERE id=? AND version>?", (identity, restore_version))
            self.db.execute("UPDATE versions SET state='active' WHERE id=? AND version=?", (identity, restore_version))
            self.event(restore, "rollback", {"from_version": expected_version})
        return self.get(identity, restore_version)

    def record_replay(self, entry, report):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.get(entry["id"], entry["version"])
            head = self.get(entry["id"])
            eligible = (report.get("eligible") is True and current["state"] in {"candidate", "active"}
                        and current["expires"] > time.time() and head["version"] == entry["version"])
            if self.origin_hashes(entry["id"]) & {c["input_sha256"] for c in report.get("cases", [])}:
                eligible = False
                report["admission_error"] = "Learning evidence now overlaps replay inputs"
            if report.get("context_revision") and report["context_revision"] != self.context_revision(entry["scope"]):
                eligible = False
                report["admission_error"] = "Active playbook changed during replay"
            if eligible:
                self.db.execute("UPDATE versions SET state='superseded' WHERE id=? AND state='active'", (entry["id"],))
                self.db.execute("UPDATE versions SET state='active' WHERE id=? AND version=?", (entry["id"], entry["version"]))
            elif current["state"] == "active" and report.get("status") == "completed" and not report.get("candidate_all_passed"):
                self.db.execute("UPDATE versions SET state='candidate' WHERE id=? AND version=?", (entry["id"], entry["version"]))
            report["promoted"] = eligible
            self.event(entry, "replay", report)
        return report

    def claim(self, key):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
            if row and row["result"] and json.loads(row["result"]).get("status") not in {"deferred", "interrupted"}:
                return None, json.loads(row["result"])
            if row and row["lease"] > time.time():
                return None, {"status": "busy"}
            owner = uuid4().hex
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,lease=excluded.lease,result=NULL",
                            (key, owner, time.time()+self.settings["timeout_seconds"]+30))
            return owner, None

    def finish_job(self, key, owner, result):
        with self.db:
            changed = self.db.execute("UPDATE jobs SET result=?,lease=0 WHERE id=? AND owner=?",
                                      (json.dumps(result, ensure_ascii=False), key, owner)).rowcount
            if changed != 1:
                raise ValueError("Learning lease lost")
