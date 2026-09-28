from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from ..harness.artifacts import ArtifactStore
from .spec import digest


class CrawlBusy(ValueError):
    pass


class CrawlPaused(Exception):
    pass


class CrawlStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "crawls.sqlite3", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS crawls(id TEXT PRIMARY KEY, state TEXT, owner TEXT, lease REAL DEFAULT 0, pause INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS origins(origin TEXT PRIMARY KEY, next_request REAL, last_request REAL DEFAULT 0);
        """)
        self.owner = uuid4().hex

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def get(self, identity, parent_id=None):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("Invalid crawl ID")
        row = self.db.execute("SELECT state,owner,lease,pause FROM crawls WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Unknown crawl ID")
        state = json.loads(row[0])
        if parent_id is not None and state["parent_run_id"] != parent_id:
            raise ValueError("Crawl does not belong to this parent task")
        if digest(state["spec"]) != state["spec_hash"]:
            raise ValueError("Stored crawl specification changed")
        state.update(active=row[1] is not None and row[2] > time.time(), pause_requested=bool(row[3]))
        return state

    def artifacts(self, identity):
        return ArtifactStore(self.root / identity / "artifacts")

    def list(self, parent_id, limit=20):
        rows = self.db.execute("SELECT id FROM crawls WHERE json_extract(state,'$.parent_run_id') IS ? ORDER BY rowid DESC LIMIT ?",
                               (parent_id, limit)).fetchall()
        return [self.get(row[0], parent_id) for row in rows]

    def create(self, spec, parent_id=None):
        if parent_id is not None and not re.fullmatch(r"[0-9a-f]{32}", parent_id):
            raise ValueError("Invalid parent run ID")
        identity = uuid4().hex
        state = {"crawl_id": identity, "parent_run_id": parent_id, "spec": spec, "spec_hash": digest(spec),
                 "status": "queued", "reason": None, "created": time.time(), "records": [], "keys": {}, "duplicates": 0,
                 "robots": {}, "limited": False, "attempt": 0, "queue": [{"url": u, "depth": 0, "status": "pending", "attempts": 0, "due": 0} for u in spec["seed_urls"]]}
        with self.db:
            self.db.execute("INSERT INTO crawls(id,state) VALUES(?,?)", (identity, json.dumps(state)))
        return state

    def acquire(self, identity, parent_id=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.get(identity, parent_id)
            if state["active"]:
                raise CrawlBusy("Crawl already running")
            if state["status"] in {"completed", "incomplete"}:
                return state, False
            for page in state["queue"]:
                if page["status"] == "fetching":
                    page["status"] = "pending"  # Only GET requests; replay uncertain reads.
            state.update(status="running", reason=None, attempt=state["attempt"]+1)
            self.db.execute("UPDATE crawls SET state=?,owner=?,lease=?,pause=0 WHERE id=?",
                            (json.dumps(state), self.owner, time.time()+30, identity))
        return state, True

    def check(self, identity):
        with self.db:
            row = self.db.execute("SELECT owner,pause FROM crawls WHERE id=?", (identity,)).fetchone()
            if not row or row[0] != self.owner:
                raise CrawlBusy("Crawl lease lost")
            if row[1]:
                raise CrawlPaused("pause_requested")
            self.db.execute("UPDATE crawls SET lease=? WHERE id=? AND owner=?", (time.time()+30, identity, self.owner))

    def save(self, state, finish=False):
        with self.db:
            changed = self.db.execute("UPDATE crawls SET state=?,owner=?,lease=? WHERE id=? AND owner=?",
                (json.dumps(state, ensure_ascii=False), None if finish else self.owner, 0 if finish else time.time()+30,
                 state["crawl_id"], self.owner))
            if changed.rowcount != 1:
                raise CrawlBusy("Crawl lease lost before checkpoint")

    def pause(self, identity, parent_id=None):
        state = self.get(identity, parent_id)
        with self.db:
            self.db.execute("UPDATE crawls SET pause=1 WHERE id=?", (identity,))
        return state

    def reserve_request(self, site, delay):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT next_request,last_request FROM origins WHERE origin=?", (site,)).fetchone()
            now = time.time()
            due = max(row[0], row[1]+delay) if row else 0
            # Only reserve a request that can start now. A cancelled wait must not
            # leave phantom reservations that push every subsequent resume back.
            if due <= now:
                self.db.execute("INSERT INTO origins VALUES(?,?,?) ON CONFLICT(origin) DO UPDATE SET next_request=excluded.next_request,last_request=excluded.last_request", (site, now+delay, now))
                return 0
            return due-now

    def cooldown(self, site, seconds):
        with self.db:
            self.db.execute("INSERT INTO origins(origin,next_request) VALUES(?,?) ON CONFLICT(origin) DO UPDATE SET next_request=MAX(next_request,excluded.next_request)", (site, time.time()+seconds))

    def records(self, state):
        artifacts = self.artifacts(state["crawl_id"])
        return [artifacts.load(ref["artifact_id"]) for ref in state["records"]]
