"""Evidence-gated procedural memory; never edits source, skills, or permissions."""
from __future__ import annotations

import json
import sqlite3
import time
from urllib.parse import urlsplit

from .task_store import task_root
from ..workflows.spec import digest


class ExperienceStore:
    def __init__(self, config):
        root = task_root(config)
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "experience.sqlite3", timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS evidence (id TEXT, run_id TEXT, host TEXT, recipe TEXT, verified INTEGER, created REAL, PRIMARY KEY(id,run_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS revoked (id TEXT PRIMARY KEY)")

    def close(self):
        self.db.close()

    def record(self, state):
        # Preserve a failed -> successful recovery as a candidate. Promotion needs
        # host verification in at least two independent runs, not model confidence.
        history = state.get("history", [])
        verified = state.get("completion_basis") == "host_verified" and state.get("verification", {}).get("ok") is True
        count = 0
        for previous, following in zip(history, history[1:]):
            if previous.get("result", {}).get("ok") is not False or following.get("result", {}).get("ok") is not True:
                continue
            url = following.get("result", {}).get("url") or following.get("url", "")
            host = urlsplit(url).netloc
            if not host:
                continue
            def action(item):
                value = item.get("action", {})
                return value.get("name") if value.get("action") == "tool" else value.get("action")
            recipe = {"failed_action": action(previous), "failure": previous["result"].get("status") or previous["result"].get("errorType", "failed"),
                      "successful_action": action(following), "guidance": "When the same failure recurs, inspect whether this previously successful alternative applies. Reobserve targets; never replay old IDs or inputs."}
            identity = digest({"host": host, **recipe})
            with self.db:
                self.db.execute("""INSERT INTO evidence VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id,run_id) DO UPDATE SET verified=MAX(evidence.verified,excluded.verified),created=excluded.created""",
                    (identity, state["run_id"], host, json.dumps(recipe), int(verified), time.time()))
            count += 1
        return {"candidate_observations": count, "host_verified": verified}

    def recall(self, url, limit=4):
        host = urlsplit(url or "").netloc
        rows = self.db.execute("""SELECT id,recipe,COUNT(DISTINCT run_id) FROM evidence
            WHERE host=? AND verified=1 AND created>? AND id NOT IN (SELECT id FROM revoked)
            GROUP BY id HAVING COUNT(DISTINCT run_id)>=2 ORDER BY MAX(created) DESC LIMIT ?""",
            (host, time.time()-30*86400, limit))
        return [{"memory_id": r[0], **json.loads(r[1]), "verified_runs": r[2]} for r in rows]

    def revoke(self, identity):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO revoked VALUES(?)", (identity,))
