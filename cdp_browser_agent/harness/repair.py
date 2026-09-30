"""Source-bound evidence and temporary advice for repairs inside a running task.

The scheduler selects actual failed decisions; it does not supply a recipe or gold
answer. Temporary reflections are separate from replay-admitted playbook entries.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .playbook import host_of, scope, settings
from .learning import preview
from ..workflows.spec import digest


def event_source(state, entry):
    action, result = entry.get("action", {}), entry.get("result", {})
    arguments = action.get("arguments", {})
    identity = result.get("source_id") or arguments.get("source_id")
    if identity:
        return identity
    job = result.get("job_id") or arguments.get("job_id")
    source_id = state.get("document_candidates", {}).get(job, {}).get("source_id")
    if source_id:
        return source_id
    artifact_id = arguments.get('artifact_id')
    if artifact_id:
        for h in state.get('history', []):
            if h.get('result', {}).get('artifact', {}).get('artifact_id') == artifact_id:
                return h['result'].get('source_id') or h.get('action', {}).get('arguments', {}).get('source_id')
    return None


def relevant_history(state, source_id):
    # Search the durable history, rather than a global recent-history window.
    return [h for h in state.get("history", []) if event_source(state, h) == source_id]


def select_events(history, limit=12):
    """Keep failure/revision decisions before filling remaining slots by recency."""
    important = [h for h in history if h.get("result", {}).get("ok") is False
                 or h.get("action", {}).get("name") in {"document_preview", "document_export", "document_review_retry"}]
    chosen = important[-limit:]
    ids = {h["actionId"] for h in chosen}
    for h in reversed(history):
        if len(chosen) >= limit:
            break
        if h["actionId"] not in ids:
            chosen.append(h)
            ids.add(h["actionId"])
    return sorted(chosen, key=lambda h: h.get("step", 0))


def document_evidence(config, state, method_hash, service, source_id, artifacts=None):
    from ..documents.review import build_evidence, _mapping_view
    history = relevant_history(state, source_id)
    if not history:
        return None
    raw, metadata = service._source(source_id)
    ledger = state.get("document_recovery", {}).get(source_id, {})
    job_id = ledger.get("latest_job_id")
    folder, receipt, candidate = service._job(job_id) if job_id else (None, {}, {})
    review = state.get("document_reviews", {}).get(job_id, {})
    events = []
    for h in select_events(history):
        result = h.get("result", {})
        if artifacts and result.get('truncated') and result.get('artifact', {}).get('artifact_id'):
            result = artifacts.load(result['artifact']['artifact_id'])
        # Semantic fields remain structured even when the tool's full result is large.
        semantic = {k: deepcopy(result[k]) for k in
                    ("ok", "status", "job_id", "source_id", "revision", "review", "errorType", "message",
                     "selector", "total", "tags", "classes", "observed_selectors", "samples") if k in result}
        if 'samples' in semantic:
            semantic['samples'] = [{k: v[:320] if k == 'text' else v for k, v in sample.items()}
                                   for sample in semantic['samples'][:3]]
        # Preserve counts/structure as structured facts even when sample bodies
        # are large. These observations are not expected labels or a repair recipe.
        samples = semantic.pop('samples', None)
        if samples is not None:
            semantic['samples'] = preview(samples, 1400)
        events.append({"id": h["actionId"], "action": preview(h.get("action"), 2400),
                       "result": preview(semantic, 3200), "url": h.get("url", "")})
    projection = build_evidence(service, receipt, candidate) if job_id else {'evidence': []}
    cited = {i.get("evidence_id") for i in review.get("issues", [])}
    evidence = [e for e in projection["evidence"] if e["id"] in cited]
    indexes = {int(m.group(1)) for identity in cited if isinstance(identity, str)
               and (m := re.fullmatch(r"(?:record|label)_(\d+)", identity))}
    for identity in cited:
        if isinstance(identity, str) and (m := re.fullmatch(r"key_diagnostic_(\d+)", identity)):
            diagnostics = projection.get('key_diagnostics', {}).get('warnings', [])
            n = int(m.group(1))
            if n < len(diagnostics) and type(diagnostics[n].get('record_index')) is int:
                indexes.add(diagnostics[n]['record_index'])
    if candidate:
        indexes.update({0, max(0, len(candidate[receipt["spec"].get("collection_key", "records")]) - 1)})
    mappings = candidate.get("source_unit_mapping", [])
    matching = [(n, m) for n, m in enumerate(mappings)
                if any(i in indexes for i in m["record_indexes"])]
    # Each sampled record index still points to its exact saved row and mapping.
    mapping_views = [_mapping_view(m, n, indexes) for n, m in matching[:6]]
    return {"source": {"run_id": state["run_id"], "attempt": state.get("attempt", 1),
                        "document_source_id": source_id, "job_id": job_id,
                        "candidate_sha256": receipt.get("candidate_sha256")},
            "scope": scope(config, "planner", method_hash, host_of(metadata.get("url", ""))),
            "task": state["task"][:4000], "events": events,
            "document": {"url": metadata.get("url"), "format": metadata.get("format"),
                         "recipe": receipt.get("spec"), "review": {k: review[k] for k in
                             ("accepted", "status", "issues", "required_changes", "review_id", "evidence_quotes_verified") if k in review},
                         "cited_source_evidence": preview(evidence, 6000), "source_unit_mapping": mapping_views,
                         "candidate_path": str(folder / "candidate.json") if folder else None,
                         "history_action_ids": [h["actionId"] for h in history],
                         "recovery": {k: ledger[k] for k in ("stage", "last_change", "candidates", "limitation",
                             "inspections", "unproductive_actions", "diagnostic_actions_used", "limits") if k in ledger}},
            "outcome": {"status": state.get("status"), "source_stage": ledger.get("stage"),
                        "accepted": review.get("accepted") is True,
                        "meaning": "A validated negative review is a repair signal; an inconclusive review establishes no defect. Output change alone is not correctness."},
            "origin_hashes": [digest({"task": state["task"]}), metadata["sha256"], source_id]}


def reflection_trigger(config, state):
    options = settings(config)
    if not options["enabled"] or not options["online_reflection"]:
        return None
    source_id = state.get("active_document_source_id")
    ledger = state.get("document_recovery", {}).get(source_id, {})
    if ledger.get("stage") not in {"revise", "diagnose"}:
        return None
    job_id = ledger.get("latest_job_id")
    review = state.get("document_reviews", {}).get(job_id, {})
    if review.get("accepted") or review.get("status") == "review_inconclusive":
        return None
    repeated = ledger.get('stage') == 'diagnose' and ledger.get('unproductive_actions', 0) >= ledger.get('limits', {}).get('unproductive_limit', 4)
    if not review.get('issues') and not repeated:
        return None
    # Cosmetic spec changes and repeated reads share one reflection identity.
    key = digest({"source": source_id, "output": ledger.get("output_fingerprint"),
                  "issues": review.get("issues", []), "policy": review.get("policy_id"),
                  "signal": 'quoted_review' if review.get('issues') else 'repeated_unproductive_inspection'})
    attempts = state.get("repair_reflections", [])
    if any(a["trigger_id"] == key for a in attempts):
        return None
    if len(attempts) >= options["max_online_reflections"]:
        return None
    if sum(a["source_id"] == source_id for a in attempts) >= options["max_source_reflections"]:
        return None
    return {"trigger_id": key, "source_id": source_id, "job_id": job_id,
            "signal": 'quoted_review' if review.get('issues') else 'repeated_unproductive_inspection'}


def temporary_advice(config, state, method_hash):
    active = state.get("active_document_source_id")
    result = []
    for reflection in reversed(state.get("repair_reflections", [])):
        if reflection["source_id"] != active or reflection.get("method_hash") != method_hash:
            continue
        if reflection.get("status") != "completed":
            continue
        for n, lesson in enumerate(reflection.get("reflections", {}).get("lessons", [])):
            result.append({"id": reflection["trigger_id"] + f":{n}", **lesson,
                           "source_id": active, "basis": "current_task_reflection",
                           "validated_for_future_tasks": False})
        break
    while len(str(result)) > settings(config)["max_context_chars"]:
        result.pop()
    return result


def record_repair_use(state, selections, entry):
    if not selections:
        return
    source_id = event_source(state, entry)
    if source_id and not any(a.get('source_id') == source_id for a in selections):
        return
    result = entry.get("result", {})
    revision = result.get("revision", {})
    if not revision and entry["action"].get("name") == "document_preview":
        source_id = event_source(state, entry)
        saved = state.get("document_recovery", {}).get(source_id, {}).get("last_change", {})
        if saved.get("job_id") == result.get("job_id"):
            revision = saved
    review = result.get("review", {})
    state.setdefault("repair_adoption", []).append({
        "step": entry["step"], "action_id": entry["actionId"],
        "advice_ids": [a["id"] for a in selections], "action": preview(entry["action"], 1600),
        "effective_change": revision.get("effective_change"),
        "changed_spec_fields": revision.get("changed_spec_fields", []),
        "changed_record_fields": revision.get("changed_record_fields", []),
        "review_accepted": review.get("accepted") if review else None,
        "meaning": "Advice reached this planner decision. The action and verified output delta are recorded separately; injection alone is not proven use or improvement."})
