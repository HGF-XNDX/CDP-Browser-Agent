"""Host-owned page assertions used by tasks and workflow agent steps."""
from __future__ import annotations


async def check_page(controller, checks: list[dict]) -> dict:
    evidence = []
    for check in checks:
        try:
            kind = check["kind"]
            if kind == "url":
                actual = controller.page.url
                ok = actual.startswith(check["prefix"])
            else:
                locator = controller.page.locator(check["selector"])
                await locator.first.wait_for(state="visible", timeout=check.get("timeout_ms", 3000))
                actual = await locator.first.inner_text(timeout=3000) if kind == "text" else "visible"
                ok = check["contains"] in actual if kind == "text" else True
            evidence.append({"check": check, "ok": ok, "actual": actual[:2000]})
        except Exception as exc:
            evidence.append({"check": check, "ok": False, "error": str(exc)[:600]})
    return {"ok": bool(checks) and all(item["ok"] for item in evidence), "checks": evidence}


def check_processing(config, state):
    """Only operator-configured delivery requirements can certify a task."""
    from ..processing.verification import verify_delivery
    from ..processing.learning import processing_root
    from ..processing.sessions import WorkerStore
    checks = config.get("agent", {}).get("completion_processing", [])
    results = []
    if not isinstance(checks, list):
        return {"ok": False, "error": "completion_processing must be a list"}
    for check in checks:
        try:
            profile = check["profile"]
            minimum = check.get("min_records", 1)
            min_turn = check.get("min_turn", 1)
            require = check.get("require_contract", True)
            if not isinstance(profile, str) or isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1 or not isinstance(require, bool) or isinstance(min_turn, bool) or not isinstance(min_turn, int) or min_turn < 1:
                raise ValueError("Invalid operator delivery requirement")
            candidates = [r for r in state.get("processing_results", []) if r.get("profile") == profile]
            if not candidates:
                raise ValueError("Required processing delivery is absent")
            result = candidates[-1]
            if result.get("turn", 1) < min_turn:
                raise ValueError("Required processing revision has not been completed")
            if result.get("worker_session_id"):
                with WorkerStore(config) as store:
                    worker = store.get(result["worker_session_id"])
                if worker["status"] != "completed" or worker["turn"] != result["turn"] or worker["active"]:
                    raise ValueError("Worker delivery is stale, active or incomplete")
                if worker["parent_run_id"] != state["run_id"] or worker["result"].get("delivery_sha256") != result.get("delivery_sha256"):
                    raise ValueError("Worker lineage or delivery identity changed")
            results.append(verify_delivery(result, processing_root(config), require_contract=require, min_records=minimum))
        except Exception as exc:
            results.append({"ok": False, "error": str(exc)[:1000]})
    return {"ok": bool(checks) and all(r["ok"] for r in results), "basis": "operator_processing_contract",
            "checks": results, "semantic_accuracy_verified": False}
