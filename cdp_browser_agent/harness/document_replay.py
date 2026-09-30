"""Execute a bounded document repair decision on an independent operator fixture.

Expectations are consumed only after real planner/tool execution. This replay checks
recipe outputs, not a complete browser workflow or cross-site generalization.
"""
from __future__ import annotations

from copy import deepcopy

from .playbook import host_of
from ..documents.engine import SPEC_SCHEMA, digest
from ..workflows.store import atomic_json


def validate_case(case, host):
    from jsonschema import Draft202012Validator
    document = case.get("document", {})
    if (not isinstance(document, dict) or not isinstance(document.get("content"), str)
            or not document["content"] or document.get("format") not in {"html", "xml", "json", "text"}
            or host_of(document.get("url")) != host):
        raise ValueError("Document replay requires source content/format and the matching public URL host")
    Draft202012Validator(SPEC_SCHEMA).validate(case.get("initial_spec"))
    expected = case["expected"]
    if set(expected) - {"keys", "text_by_key"}:
        raise ValueError("Document replay expectations support keys and optional text_by_key only")
    keys = expected.get("keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        raise ValueError("Document replay requires nonempty expected keys")
    text = expected.get("text_by_key", {})
    if not isinstance(text, dict) or not all(isinstance(v, str) and k in keys for k, v in text.items()):
        raise ValueError("Invalid document replay expected body text")


def check_candidate(service, state, source_id, expected):
    ledger = state.get("document_recovery", {}).get(source_id, {})
    job_id = ledger.get("latest_job_id")
    _, receipt, candidate = service._job(job_id)
    rows = candidate[receipt["spec"].get("collection_key", "records")]
    review = state.get("document_reviews", {}).get(job_id, {})
    checks = {"recipe_valid": candidate["validation"]["ok"] is True,
              "tree_conserved": candidate["coverage"]["tree_conserved"] is True,
              "review_accepted": review.get("accepted") is True,
              "keys_match": [r["key"] for r in rows] == expected["keys"],
              "bodies_match": all(r["text"] == expected["text_by_key"][r["key"]]
                                  for r in rows if r["key"] in expected.get("text_by_key", {}))}
    return {"passed": all(checks.values()), "checks": checks, "job_id": job_id,
            "candidate_sha256": receipt["candidate_sha256"], "recipe": receipt["spec"],
            "keys": [r["key"] for r in rows]}


async def evaluate_case(config, runtime, model, case, values, folder, max_steps=3):
    from ..browser.planner import plan_next_action
    from ..browser.policy import validate_action
    if type(max_steps) is not int or not 1 <= max_steps <= 5:
        raise ValueError("Document replay max_steps must be an integer in 1..5")
    folder.mkdir(parents=True, exist_ok=True)
    previous_state = runtime.task_state
    raw = case["document"]["content"].encode("utf-8")
    metadata = {"url": case["document"]["url"], "origin": "operator_replay_fixture"}
    opened = runtime.documents._save_source(raw, metadata, case["document"]["format"])
    source_id = opened["source_id"]
    state = {"run_id": "replay-" + digest(str(folder))[:16], "task": case["task"], "step": 0,
             "status": "running", "history": [], "sources": [], "collected_files": [],
             "document_sources": {source_id: {k: opened[k] for k in ("source_id", "url", "format", "bytes")}}}
    runtime.task_state = state
    decisions = []
    try:
        # The initial recipe reconstructs the observed decision point. Its review
        # uses source evidence and the task, with no operator expected answer.
        initial_action = {"action": "tool", "name": "document_preview",
                          "arguments": {"source_id": source_id, "spec": deepcopy(case["initial_spec"])}}
        initial = await runtime.registry.call(initial_action["name"], initial_action["arguments"])
        state["last_result"] = initial
        state["history"].append({"actionId": "R0000", "step": 0, "action": initial_action, "result": initial})
        allowed = {"document_inspect", "document_preview", "document_review", "document_review_retry",
                   "artifact_read", "artifact_search", "tool_describe", "history_read"}
        report = {"passed": False, "initial_job_id": initial.get("job_id")}
        for step in range(1, max_steps + 1):
            state["step"] = step
            request = {"task": case["task"], "step": step, "observation": deepcopy(case["observation"]),
                       "last_result": state["last_result"], "page_context": {"pages": []},
                       "model_settings": model, "agent_settings": config.get("agent", {}),
                       "extensions": runtime.context(), "memory_context": {"playbook_advice": values,
                            "recent_exact_history": deepcopy(state["history"][-5:])}, "browser_started": False}
            generated = await plan_next_action(request)
            action = validate_action(generated["action"], request["observation"], request)
            if action["action"] != "tool" or action["name"] not in allowed:
                decisions.append({"action": action, "not_executed": "Outside the bounded document-repair replay contract"})
                break
            result = await runtime.registry.call(action["name"], action["arguments"])
            entry = {"actionId": f"R{step:04d}", "step": step, "action": action, "result": result}
            state["history"].append(entry)
            state["last_result"] = result
            decisions.append(entry)
            if action["name"] == "document_preview" and result.get("job_id"):
                report.update(check_candidate(runtime.documents, state, source_id, case["expected"]))
                if report["passed"]:
                    break
        # Expected values are read only here/after executed candidate generation.
        report.update(check_candidate(runtime.documents, state, source_id, case["expected"]))
        report["source_unchanged"] = runtime.documents._source(source_id)[0] == raw
        report["passed"] = report["passed"] and report["source_unchanged"]
        report["decision_count"] = len(decisions)
        report["meaning"] = "Executed recipe repair on an operator fixture; not a full independent browser task. Baseline and candidate differ only in retained playbook context."
        atomic_json(folder / "trace.json", {"decisions": decisions, "state": state})
        atomic_json(folder / "report.json", report)
        return report
    finally:
        runtime.task_state = previous_state
