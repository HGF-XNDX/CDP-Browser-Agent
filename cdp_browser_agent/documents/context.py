"""Recoverable planner views centered on the current document source."""
from __future__ import annotations

from copy import deepcopy
import json


def project_context(payload, artifacts, settings, execution_budget):
    focus = payload.get('extensions', {}).get('document_focus', {})
    active = focus.get('active_source_id')
    if not active and not focus.get('pending_url'):
        return payload, {'status': 'not_applicable'}
    archive = artifacts.save(payload)
    view = deepcopy(payload)
    latest_job = (focus.get('active_candidate') or {}).get('job_id')
    identities = [active or focus['pending_url']] + ([latest_job] if latest_job else [])
    history = payload.get('recent_history', []) + payload.get('recalled_relevant_history', [])
    selected, seen = [], set()
    for item in history:
        identity = item.get('action_id') or item.get('actionId') or json.dumps(item, sort_keys=True, ensure_ascii=False)
        if identity in seen:
            continue
        seen.add(identity)
        if any(key in json.dumps(item, ensure_ascii=False) for key in identities):
            selected.append(item)
    # Retain the newest operation even when it changed sources or used a general
    # diagnostic tool. Full records remain available through the archive/IDs.
    for item in payload.get('recent_history', [])[-2:]:
        if item not in selected:
            selected.append(item)
    selected.sort(key=lambda item: history.index(item))
    selected = selected[-8:]
    char_limit = int(settings.get('document_history_chars', 18000))
    if not 2000 <= char_limit <= 100000:
        raise ValueError('document_history_chars must be between 2000 and 100000')
    while len(selected) > 1 and len(json.dumps(selected, ensure_ascii=False)) > char_limit:
        selected.pop(0)
    view.update(recent_history=selected, recalled_relevant_history=[], compressed_action_memory=[],
        task_memory={'active_source_id': active, 'pending_url': focus.get('pending_url'),
            'stage': (focus.get('recovery') or focus.get('retrieval') or {}).get('stage'),
            'full_task_memory': {'artifact_id': archive['artifact_id'], 'field': 'task_memory'}},
        run_memory_brief={'archived': {'artifact_id': archive['artifact_id'], 'field': 'run_memory_brief'}},
        context_archive=archive, execution_budget=execution_budget)
    active_source = next((s for s in view['extensions'].get('document_sources', []) if s['source_id'] == active), {})
    view['sources'] = [s for s in view.get('sources', []) if s.get('url') == active_source.get('url')]
    target = int(settings.get('document_prompt_target_tokens', 18000))
    if not 6000 <= target <= 100000:
        raise ValueError('document_prompt_target_tokens must be between 6000 and 100000')
    slow = execution_budget.get('previous_plan_seconds')
    step_target = float(settings.get('document_plan_seconds', 35))
    if step_target <= 0:
        raise ValueError('document_plan_seconds must be positive')
    if slow and slow > step_target:
        target = max(10000, int(target * max(.5, step_target / slow)))
    remaining = execution_budget.get('remaining_seconds')
    if remaining is not None and remaining < 2 * step_target:
        target = min(target, 10000)
    receipt = {'status': 'source_focused', 'source_id': active, 'original': archive,
        'before_chars': len(json.dumps(payload, ensure_ascii=False)),
        'after_chars': len(json.dumps(view, ensure_ascii=False)), 'target_prompt_tokens': target,
        'history_action_ids': [r.get('action_id') or r.get('actionId') for r in selected],
        'execution_budget': execution_budget,
        'preserved': 'Task, decisions, tool schemas, current candidate/recipe/defects and last result stay active. Original context and full history remain recoverable.'}
    view['document_context_projection'] = receipt
    return view, receipt
