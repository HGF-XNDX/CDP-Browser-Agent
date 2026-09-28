"""Procedural advice promoted only by paired, operator-owned held-out replay."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from ..workflows.spec import digest
from ..workflows.store import atomic_json
from .verification import verify_delivery


def processing_root(config):
    return Path(config.get("processing", {}).get("artifact_dir", "downloads/processed")).resolve()


class ProcedureStore:
    def __init__(self, config):
        self.root = processing_root(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "learning.sqlite3", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS procedures (
              id TEXT PRIMARY KEY, method TEXT, profile TEXT, guidance TEXT,
              state TEXT, origins TEXT, updated REAL, replay TEXT);
            CREATE TABLE IF NOT EXISTS replays (id TEXT PRIMARY KEY, procedure_id TEXT, report TEXT);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def get(self, identity):
        row = self.db.execute("SELECT * FROM procedures WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Unknown processing experience")
        return dict(zip(("id", "method_hash", "profile", "guidance", "state", "origins", "updated", "replay"),
                        (*row[:5], json.loads(row[5]), row[6], json.loads(row[7]) if row[7] else None)))

    def list(self, profile=None):
        rows = self.db.execute("SELECT id FROM procedures WHERE (? IS NULL OR profile=?) ORDER BY updated DESC LIMIT 50", (profile, profile))
        return [self.get(row[0]) for row in rows]

    def propose(self, method_hash, profile, guidance, session_id, turn, input_hashes):
        if not isinstance(guidance, str) or not guidance.strip() or len(guidance) > 4000:
            raise ValueError("Processing guidance must have 1..4000 characters")
        identity = digest({"method": method_hash, "guidance": guidance.strip()})
        origin = {"session_id": session_id, "turn": turn, "input_hashes": sorted(set(input_hashes))}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT origins FROM procedures WHERE id=?", (identity,)).fetchone()
            origins = json.loads(row[0]) if row else []
            if origin not in origins:
                origins.append(origin)
            self.db.execute("""INSERT INTO procedures VALUES(?,?,?,?,'candidate',?,?,NULL)
                ON CONFLICT(id) DO UPDATE SET origins=excluded.origins""",
                (identity, method_hash, profile, guidance.strip(), json.dumps(origins), time.time()))
        return identity

    def recall(self, method_hash, limit=2):
        rows = self.db.execute("""SELECT id,guidance FROM procedures WHERE method=? AND state='promoted'
            AND updated>? ORDER BY updated DESC LIMIT ?""", (method_hash, time.time()-30*86400, limit))
        return [{"id": row[0], "guidance": row[1], "basis": "paired_heldout_replay"} for row in rows]

    def revoke(self, identity):
        self.get(identity)
        with self.db:
            self.db.execute("UPDATE procedures SET state='revoked',updated=? WHERE id=?", (time.time(), identity))
        return self.get(identity)

    def _record_replay(self, identity, report):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.get(identity)
            origin_inputs = {value for origin in current["origins"] for value in origin["input_hashes"]}
            overlap = bool(origin_inputs & {r["input_sha256"] for r in report.get("cases", [])})
            promote = bool(report.get("eligible")) and not overlap
            report["promoted"] = promote and current["state"] != "revoked"
            self.db.execute("INSERT INTO replays VALUES(?,?,?)", (report["replay_id"], identity, json.dumps(report)))
            # A concurrent revoke always wins. A failed new replay demotes advice.
            if report["status"] != "deferred":
                self.db.execute("UPDATE procedures SET state=?,updated=?,replay=? WHERE id=? AND state!='revoked'",
                    ("promoted" if promote else "candidate", time.time(), json.dumps(report), identity))
        return report


class ReplayCatalog:
    def __init__(self, config):
        self.suites = {}
        for root in map(Path, config.get("processing", {}).get("replay_paths", [])):
            if not root.exists():
                raise ValueError("Replay suite path does not exist")
            for path in ([root] if root.is_file() else sorted(root.glob("*.json"))):
                if path.stat().st_size > 2_000_000:
                    raise ValueError("Replay suite too large")
                suite = json.loads(path.read_text(encoding="utf-8-sig"))
                if not re.fullmatch(r"[\w-]{1,64}", suite.get("name", "")) or suite["name"] in self.suites:
                    raise ValueError("Invalid or duplicate replay suite name")
                cases = suite.get("cases", [])
                if not isinstance(cases, list) or not all(isinstance(c, dict) and isinstance(c.get("expected"), dict)
                        and isinstance(c.get("record"), dict) and isinstance(c["record"].get("data"), dict) for c in cases):
                    raise ValueError("Replay cases require records and operator expected objects")
                if not 2 <= len(cases) <= 10 or len({digest(c["record"]["data"]) for c in cases}) != len(cases):
                    raise ValueError("Replay needs 2..10 distinct input cases")
                if not isinstance(suite.get("profile"), str) or not suite["profile"]:
                    raise ValueError("Replay suite requires a registered profile")
                self.suites[suite["name"]] = suite

    def catalog(self):
        return [{"name": s["name"], "profile": s["profile"], "case_count": len(s["cases"])} for s in self.suites.values()]


async def replay_experience(config, identity, suite_name):
    from .engine import ProcessingEngine
    from .sessions import ProcessingSessions, WorkerStore, WorkerBusy
    ProcessingSessions(config)  # Validate the shared worker/replay admission limits.
    timeout = config.get("processing", {}).get("replay_timeout_seconds", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("Replay timeout must be finite and in (0, 3600] seconds")
    catalog = ReplayCatalog(config)
    if suite_name not in catalog.suites:
        raise ValueError("Unknown operator replay suite")
    suite = catalog.suites[suite_name]
    with ProcedureStore(config) as store:
        proposal = store.get(identity)
        if proposal["state"] == "revoked" or suite["profile"] != proposal["profile"]:
            raise ValueError("Revoked advice or mismatched replay profile")
        origins = {v for o in proposal["origins"] for v in o["input_hashes"]}
        if any(digest(case["record"]["data"]) in origins for case in suite["cases"]):
            raise ValueError("Replay cases overlap candidate origins; supply held-out inputs")
        engine = ProcessingEngine(config)
        *_, method_hash = await engine.prepare_method(proposal["profile"])
        if method_hash != proposal["method_hash"]:
            raise ValueError("Experience method/model/skills changed; replay is not comparable")
        run_id = uuid4().hex
        folder = store.root / "replays" / run_id
        folder.mkdir(parents=True)
        atomic_json(folder / "suite.json", suite)
        report = {"replay_id": run_id, "experience_id": identity, "method_hash": method_hash,
                  "suite_sha256": digest(suite), "cases": [], "eligible": False, "promoted": False,
                  "status": "running", "semantic_accuracy_verified": False}

        async def execute():
            for index, case in enumerate(suite["cases"]):
                comparisons = {}
                for variant in ("baseline", "candidate"):
                    if store.get(identity)["state"] == "revoked":
                        raise ValueError("Experience was revoked during replay")
                    result = await engine.run(proposal["profile"], [case["record"]], folder / str(index) / variant,
                        context={"procedural_advice": ([{"id": identity, "guidance": proposal["guidance"]}] if variant == "candidate" else [])},
                        expected_method_hash=method_hash, use_experience=False)
                    verification = verify_delivery(result, store.root)
                    rows = json.loads(Path(result["records_path"]).read_text(encoding="utf-8"))
                    comparisons[variant] = {"passed": verification["ok"] and len(rows) == 1 and digest(rows[0]["data"]) == digest(case["expected"]),
                        "verification": verification, "result": result}
                report["cases"].append({"input_sha256": digest(case["record"]["data"]), **comparisons})
                atomic_json(folder / "report.json", report)
            report.update(status="completed", eligible=all(c["candidate"]["passed"] for c in report["cases"])
                          and any(not c["baseline"]["passed"] for c in report["cases"]))

        async def admitted_execute():
            with WorkerStore(config) as lease:
                key = "replay:" + identity
                lease.reserve_operation(key)
                operation = asyncio.create_task(execute())
                try:
                    while not operation.done():
                        await asyncio.wait({operation}, timeout=.25)
                        lease.checkpoint_operation(key)
                    await operation
                finally:
                    if not operation.done():
                        operation.cancel()
                    try:
                        await operation
                    finally:
                        lease.release_operation(key)
        try:
            await asyncio.wait_for(admitted_execute(), timeout)
        except BaseException as exc:
            report.update(status="deferred" if isinstance(exc, WorkerBusy) else "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed", error=type(exc).__name__)
            store._record_replay(identity, report)
            atomic_json(folder / "report.json", report)
            raise
        store._record_replay(identity, report)
        atomic_json(folder / "report.json", report)
        return {**report, "report_path": str(folder / "report.json")}
