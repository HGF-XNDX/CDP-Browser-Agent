"""Durable, bounded processing conversations with parent linkage and cancellation."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from .engine import ProcessingEngine
from .learning import ProcedureStore, processing_root, ReplayCatalog, replay_experience
from ..harness.artifacts import ArtifactStore
from ..workflows.spec import digest


class WorkerBusy(ValueError):
    pass


class WorkerStore:
    def __init__(self, config):
        self.config = config
        self.root = processing_root(config) / "workers"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "workers.sqlite3", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workers (
              id TEXT PRIMARY KEY, state TEXT, owner TEXT, lease REAL DEFAULT 0, cancel INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, worker TEXT, kind TEXT, payload TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, owner TEXT, lease REAL);
        """)
        self.owner = uuid4().hex

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def get(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("Invalid worker session ID")
        row = self.db.execute("SELECT state,owner,lease,cancel FROM workers WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Unknown worker session")
        state = json.loads(row[0])
        state["active"] = row[1] is not None and row[2] > time.time()
        state["cancel_requested"] = bool(row[3])
        return state

    def event(self, identity, kind, payload):
        self.db.execute("INSERT INTO events(worker,kind,payload,created) VALUES(?,?,?,?)",
                        (identity, kind, json.dumps(payload, ensure_ascii=False), time.time()))

    def create(self, profile, method_hash, frozen, records, parent_id=None):
        if parent_id is not None and not re.fullmatch(r"[0-9a-f]{32}", parent_id):
            raise ValueError("Invalid parent run ID")
        identity = uuid4().hex
        reference = ArtifactStore(self.root / identity / "artifacts").save({"records": records, "method": frozen})
        state = {"worker_session_id": identity, "parent_run_id": parent_id, "profile": profile,
                 "method_hash": method_hash, "input": reference, "status": "queued", "turn": 1,
                 "feedback": "", "feedback_origin": "initial_request", "pending": True,
                 "attempts": 0, "contexts": {}, "result": None, "result_turn": 0, "created": time.time()}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if parent_id:
                count = sum(json.loads(r[0]).get("parent_run_id") == parent_id for r in self.db.execute("SELECT state FROM workers"))
                if count >= int(self.config.get("processing", {}).get("max_children_per_task", 8)):
                    raise ValueError("Parent task reached its processing session limit")
            self.db.execute("INSERT INTO workers(id,state) VALUES(?,?)", (identity, json.dumps(state)))
            self.event(identity, "created", {"input": reference, "method_hash": method_hash, "parent_run_id": parent_id})
        return state

    def acquire(self, identity, feedback, expected_turn, origin, parent_id=None):
        settings = self.config.get("processing", {})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.get(identity)
            if parent_id is not None and state["parent_run_id"] != parent_id:
                raise ValueError("Worker does not belong to this parent task")
            if state["active"]:
                raise WorkerBusy("Worker already running")
            if feedback is not None:
                if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4000:
                    raise ValueError("Feedback must have 1..4000 characters")
                if expected_turn != state["turn"]:
                    raise ValueError("Stale/missing expected_turn; inspect worker status before sending feedback")
                if state["status"] == "completed" and feedback.strip() == state["feedback"].strip():
                    return {**state, "feedback_already_applied": True}, False
                if state["pending"]:
                    raise ValueError("Resume or cancel the pending turn before appending feedback")
                if state["turn"] >= int(settings.get("worker_max_turns", 5)):
                    raise ValueError("Worker turn budget exhausted")
                state.update(turn=state["turn"]+1, feedback=feedback, feedback_origin=origin,
                             pending=True, attempts=0, status="queued")
                self.db.execute("UPDATE workers SET cancel=0 WHERE id=?", (identity,))
                self.event(identity, "message", {"turn": state["turn"], "feedback": feedback, "origin": origin})
            elif not state["pending"]:
                return state, False
            if state["attempts"] >= int(settings.get("worker_max_attempts", 3)):
                raise ValueError("Worker attempt budget exhausted; cancel this turn to proceed")
            active = self.active_count()
            if active >= int(settings.get("max_concurrent_sessions", 1)):
                raise WorkerBusy("Processing concurrency limit reached")
            state.update(status="running", attempts=state["attempts"]+1, error=None)
            self.db.execute("UPDATE workers SET state=?,owner=?,lease=? WHERE id=?",
                            (json.dumps(state), self.owner, time.time()+30, identity))
            self.event(identity, "turn_started", {"turn": state["turn"], "attempt": state["attempts"]})
            return state, True

    def active_count(self):
        now = time.time()
        return (self.db.execute("SELECT COUNT(*) FROM workers WHERE owner IS NOT NULL AND lease>?", (now,)).fetchone()[0]
                + self.db.execute("SELECT COUNT(*) FROM operations WHERE lease>?", (now,)).fetchone()[0])

    def reserve_operation(self, identity):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM operations WHERE id=? AND lease>?", (identity, time.time())).fetchone():
                raise WorkerBusy("This processing replay is already running")
            if self.active_count() >= int(self.config.get("processing", {}).get("max_concurrent_sessions", 1)):
                raise WorkerBusy("Processing concurrency limit reached")
            self.db.execute("INSERT INTO operations VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,lease=excluded.lease",
                            (identity, self.owner, time.time()+30))

    def checkpoint_operation(self, identity):
        with self.db:
            changed = self.db.execute("UPDATE operations SET lease=? WHERE id=? AND owner=?", (time.time()+30, identity, self.owner))
            if changed.rowcount != 1:
                raise RuntimeError("Processing replay lease lost")

    def release_operation(self, identity):
        with self.db:
            self.db.execute("DELETE FROM operations WHERE id=? AND owner=?", (identity, self.owner))

    def checkpoint(self, identity):
        with self.db:
            row = self.db.execute("SELECT owner,cancel FROM workers WHERE id=?", (identity,)).fetchone()
            if not row or row[0] != self.owner:
                raise RuntimeError("Worker lease lost")
            if row[1]:
                raise asyncio.CancelledError("Worker cancellation requested")
            self.db.execute("UPDATE workers SET lease=? WHERE id=? AND owner=?", (time.time()+30, identity, self.owner))

    def save_running(self, state):
        with self.db:
            changed = self.db.execute("UPDATE workers SET state=? WHERE id=? AND owner=?",
                (json.dumps(state, ensure_ascii=False), state["worker_session_id"], self.owner))
            if changed.rowcount != 1:
                raise RuntimeError("Worker lease lost")
            self.event(state["worker_session_id"], "context_frozen", {"turn": state["turn"], "context": state["contexts"][str(state["turn"])]})

    def list(self, parent_id):
        return [self.get(row[0]) for row in self.db.execute(
            "SELECT id FROM workers WHERE json_extract(state,'$.parent_run_id') IS ? ORDER BY rowid DESC LIMIT 50", (parent_id,))]

    def finish(self, state, event, before_commit=None):
        identity = state["worker_session_id"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT owner,cancel FROM workers WHERE id=?", (identity,)).fetchone()
            if not row or row[0] != self.owner:
                raise RuntimeError("Worker lease lost before commit")
            if row[1] and event == "turn_finished":
                state.update(status="cancelled", pending=False, candidate_experience_id=None)
                event = "turn_cancelled"
            elif before_commit:
                before_commit()
            changed = self.db.execute("UPDATE workers SET state=?,owner=NULL,lease=0 WHERE id=? AND owner=?",
                                      (json.dumps(state, ensure_ascii=False), identity, self.owner))
            if changed.rowcount != 1:
                raise RuntimeError("Worker lease lost before commit")
            self.event(identity, event, {"turn": state["turn"], "status": state["status"], "result": state.get("result"), "error": state.get("error")})

    def cancel(self, identity, parent_id=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.get(identity)
            if parent_id is not None and state["parent_run_id"] != parent_id:
                raise ValueError("Worker does not belong to this parent task")
            if not state["pending"]:
                return state
            self.db.execute("UPDATE workers SET cancel=1 WHERE id=?", (identity,))
            if not state["active"]:
                state.update(status="cancelled", pending=False)
                self.db.execute("UPDATE workers SET state=? WHERE id=?", (json.dumps(state), identity))
            self.event(identity, "cancel_requested", {"turn": state["turn"]})
            return self.get(identity)

    def events(self, identity, after=0, limit=20):
        self.get(identity)
        if after < 0 or not 1 <= limit <= 50:
            raise ValueError("Invalid worker event pagination")
        return [{"seq": r[0], "kind": r[1], "payload": json.loads(r[2]), "created": r[3]} for r in
                self.db.execute("SELECT seq,kind,payload,created FROM events WHERE worker=? AND seq>? ORDER BY seq LIMIT ?", (identity, after, limit))]


class ProcessingSessions:
    def __init__(self, config):
        self.config = config
        settings = config.get("processing", {})
        for key, default, maximum in (("worker_max_turns", 5, 20), ("worker_max_attempts", 3, 10),
                ("max_children_per_task", 8, 100), ("max_concurrent_sessions", 1, 4)):
            value = settings.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"Invalid processing {key}")

    def list(self, parent_id=None):
        with WorkerStore(self.config) as store:
            return [self.public(state) for state in store.list(parent_id)]

    async def create(self, profile, records, *, parent_id=None):
        engine = ProcessingEngine(self.config)
        method, _, _, frozen, method_hash = await engine.prepare_method(profile)
        if not isinstance(records, list) or not 1 <= len(records) <= method.get("max_records", 500):
            raise ValueError("Worker records must be a nonempty bounded list")
        if not all(isinstance(r, dict) and isinstance(r.get("data"), dict) for r in records):
            raise ValueError("Worker records require data objects")
        records = deepcopy(records)
        for index, record in enumerate(records):
            record["record_key"] = str(record.get("record_key", index))
        if len({r["record_key"] for r in records}) != len(records):
            raise ValueError("Worker record keys must be unique")
        with WorkerStore(self.config) as store:
            return self.public(store.create(profile, method_hash, frozen, records, parent_id))

    @staticmethod
    def public(state):
        current = state.get("result") if state.get("result_turn") == state["turn"] else None
        return {**(current or {}), **{k: state.get(k) for k in ("worker_session_id", "parent_run_id", "profile", "turn", "status", "pending", "attempts", "error", "candidate_experience_id")},
                "applied_feedback": state.get("feedback", ""), "feedback_already_applied": state.get("feedback_already_applied", False),
                "ok": state["status"] == "completed", "active": state.get("active", False),
                "cancel_requested": state.get("cancel_requested", False)}

    def status(self, identity, *, parent_id=None, after=0):
        with WorkerStore(self.config) as store:
            state = store.get(identity)
            if parent_id is not None and state["parent_run_id"] != parent_id:
                raise ValueError("Worker does not belong to this parent task")
            events = store.events(identity, after)
            return {**self.public(state), "events": events, "event_cursor": events[-1]["seq"] if events else after}

    def cancel(self, identity, *, parent_id=None):
        with WorkerStore(self.config) as store:
            return self.public(store.cancel(identity, parent_id))

    async def run(self, identity, *, feedback=None, expected_turn=None, parent_id=None, origin="caller"):
        with WorkerStore(self.config) as store:
            state, admitted = store.acquire(identity, feedback, expected_turn, origin, parent_id)
            if not admitted:
                return self.public(state)
            operation = None
            previous_result = state.get("result")
            try:
                source = ArtifactStore(store.root / identity / "artifacts").load(state["input"]["artifact_id"])
                records = source["records"]
                folder = store.root / identity / "turns" / str(state["turn"])
                artifacts = ArtifactStore(store.root / identity / "artifacts")
                frozen_context = state.get("contexts", {}).get(str(state["turn"]))
                if frozen_context:
                    context = artifacts.load(frozen_context["artifact_id"])
                    # Resume the same frozen prompt; a revoked experience blocks reuse.
                    with ProcedureStore(self.config) as learned:
                        if any(learned.get(a["id"])["state"] == "revoked" for a in context.get("procedural_advice", [])):
                            raise ValueError("Frozen turn used revoked advice; cancel it and send fresh feedback")
                else:
                    previous = {}
                    for turn in range(max(1, state["turn"]-2), state["turn"]):
                        for filename in ("validated-records.json", "failures.json"):
                            path = store.root / identity / "turns" / str(turn) / filename
                            if path.exists():
                                for item in json.loads(path.read_text(encoding="utf-8")):
                                    draft = {k: item[k] for k in ("data", "evidence", "error", "verification", "candidate") if k in item}
                                    previous.setdefault(item["record_key"], []).append({"turn": turn, **draft})
                    with ProcedureStore(self.config) as learned:
                        advice = learned.recall(state["method_hash"])
                    context = {"feedback": state["feedback"], "feedback_origin": state["feedback_origin"], "previous": previous,
                               "procedural_advice": advice}
                    state.setdefault("contexts", {})[str(state["turn"])] = artifacts.save(context)
                    store.save_running(state)
                engine = ProcessingEngine(self.config)
                operation = asyncio.create_task(engine.run(state["profile"], records, folder,
                    checkpoint=lambda: store.checkpoint(identity), context=context, expected_method_hash=state["method_hash"], use_experience=False))
                while not operation.done():
                    await asyncio.wait({operation}, timeout=.25)
                    store.checkpoint(identity)
                result = await operation
                state.update(result=result, result_turn=state["turn"], status=result["status"], pending=False,
                             candidate_experience_id=None)
                def propose():
                    if state["feedback"] and previous_result and result.get("contract_verified"):
                        from .verification import verify_delivery
                        if verify_delivery(result, processing_root(self.config))["ok"]:
                            with ProcedureStore(self.config) as learned:
                                state["candidate_experience_id"] = learned.propose(state["method_hash"], state["profile"], state["feedback"],
                                    identity, state["turn"], [digest(r["data"]) for r in records])
                store.finish(state, "turn_finished", before_commit=propose)
            except BaseException as exc:
                if operation and not operation.done():
                    operation.cancel()
                    try:
                        await operation
                    except BaseException:
                        pass
                cancelled = store.get(identity)["cancel_requested"]
                state.update(status="cancelled" if cancelled else "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                             pending=not cancelled, error=f"{type(exc).__name__}: {str(exc)[:1000]}")
                store.finish(state, "turn_interrupted")
                if isinstance(exc, asyncio.CancelledError) and not cancelled:
                    raise
            response = self.public(store.get(identity))
        if response.get("candidate_experience_id") and self.config.get("processing", {}).get("auto_replay", False):
            try:
                suites = [s for s in ReplayCatalog(self.config).catalog() if s["profile"] == state["profile"]]
                if suites:
                    response["experience_replay"] = await replay_experience(self.config, response["candidate_experience_id"], suites[0]["name"])
            except Exception as exc:
                # The delivery is already committed; a replay failure cannot undo it.
                response["experience_replay"] = {"promoted": False, "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
        return response
