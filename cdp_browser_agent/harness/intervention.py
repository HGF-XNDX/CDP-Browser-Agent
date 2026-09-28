from __future__ import annotations

import asyncio
import math
import time
from uuid import uuid4


async def handle_intervention(session, config, message):
    policy = config.get("intervention", {})
    mode = policy.get("mode", "return")
    if mode not in {"auto", "wait", "return"}:
        raise ValueError("intervention.mode must be auto, wait, or return")
    if mode == "return":
        return False
    timeout = float(policy.get("wait_seconds", 120))
    if not math.isfinite(timeout) or not 0 <= timeout <= 3600:
        raise ValueError("intervention.wait_seconds must be 0..3600")
    decision = {"question": message, "reason": "preauthorized", "mode": "auto"}
    if mode == "wait":
        pending = {"id": uuid4().hex, "question": message, "deadline": time.time() + timeout}
        session.state.update(status="waiting_input", pending_input=pending)
        session.checkpoint()
        session.recorder.write("input_requested", pending)
        while time.time() < pending["deadline"]:
            answer = session.store.consume(session.state["run_id"], pending["id"])
            if answer:
                decision.update(mode="user", reason="user_response", answer=answer)
                break
            await asyncio.sleep(min(.5, max(0, pending["deadline"]-time.time())))
        else:
            decision["reason"] = "wait_timeout"
        session.state.pop("pending_input", None)
        session.state["status"] = "running"
    session.state.setdefault("decisions", []).append(decision)
    session.recorder.write("intervention_decision", decision)
    if decision["mode"] == "auto":
        count = sum(d["mode"] == "auto" for d in session.state["decisions"])
        if count > int(policy.get("max_auto_decisions", 3)):
            session.state.update(status="incomplete", stopped_reason="decision_limit",
                                 answer="Autonomous alternatives exhausted; retained partial results and checkpoint.")
            return True
        decision["instruction"] = (
            "Choose the next action yourself within the user's task and configured tools. State your assumption in the final answer. "
            "Prefer an available source, a reversible alternative, or delivering verified partial results. "
            "Do not invent missing facts, credentials or user approval for unrelated consequential actions. "
            "Do not bypass login/CAPTCHA or blindly replay uncertain side effects. If no feasible alternative remains, finish incomplete/blocked.")
    session.state["last_result"] = {"ok": True, "intervention": decision}
    session.checkpoint()
    return True
