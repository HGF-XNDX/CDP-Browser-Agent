"""Opt-in controlled ACE document loop and independent executed replay.

The initial faulty recipe is an operator fixture, not a natural model error.
Private expected values are used only by post-generation checks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.browser.planner import plan_next_action
from cdp_browser_agent.browser.policy import validate_action
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.harness.learning import LearningService
from cdp_browser_agent.harness.playbook import PlaybookStore, planner_method, scope
from cdp_browser_agent.harness.repair import document_evidence, reflection_trigger, temporary_advice, record_repair_use
from cdp_browser_agent.model_client import prepare_model_options, RUN_METRICS
from cdp_browser_agent.workflows.store import atomic_json
from cdp_browser_agent.documents.engine import digest
from scripts.live_document_eval_support import trace_model, snapshot, model_costs


INITIAL_SPEC = {'mode': 'elements', 'selector': './/Unit', 'title_selector': 'Name',
                'key_source': {'attribute': 'Code'},
                'key_transforms': [{'operation': 'capture', 'pattern': '^([0-9]+)'}]}
HOST = 'catalog.example'


def fixture(code, title, body):
    return {'content': f'<Catalog><Unit Code="{code}"><Name>{title}</Name><Body>{body}</Body></Unit></Catalog>',
            'format': 'xml', 'url': f'https://{HOST}/{title.lower().replace(" ", "-")}'}


def task(url):
    return f'把 {url} 中目录单元的每个原始编号保存为独立对象，完整保留名称和正文；同一源单元声明多个编号时，每个编号分别对应一条记录。只根据本次源文档确定编号。'


def fresh_state(runtime, document):
    opened = runtime.documents._save_source(document['content'].encode(),
        {'url': document['url'], 'origin': 'controlled_live_fixture'}, document['format'])
    state = {'run_id': uuid4().hex, 'task': task(document['url']), 'step': 0, 'status': 'running',
             'sources': [], 'history': [], 'collected_files': [],
             'document_sources': {opened['source_id']: opened}, 'active_document_source_id': opened['source_id']}
    runtime.task_state = state
    return state, opened


async def generate(config, runtime, model, playbook, repair, max_steps=4):
    state = runtime.task_state
    allowed = {'document_preview', 'document_inspect', 'document_review', 'document_review_retry',
               'artifact_read', 'artifact_search', 'history_read', 'tool_describe', 'document_export'}
    for _ in range(max_steps):
        state['step'] += 1
        observation = {'url': next(iter(state['document_sources'].values()))['url'], 'elements': [], 'fullText': ''}
        request = {'task': state['task'], 'step': state['step'], 'observation': observation, 'last_result': state.get('last_result'),
            'page_context': {'pages': []}, 'model_settings': model, 'agent_settings': config['agent'],
            'extensions': runtime.context(), 'memory_context': {'playbook_advice': playbook, 'repair_advice': repair,
                'recent_exact_history': state['history'][-6:]}, 'browser_started': False}
        planned = await plan_next_action(request)
        action = validate_action(planned['action'], observation, request)
        if action['action'] != 'tool' or action['name'] not in allowed:
            state['stop_action'] = action
            break
        result = await runtime.registry.call(action['name'], action['arguments'])
        entry = {'actionId': f'A{state["step"]:04d}', 'step': state['step'], 'action': action,
                 'result': result, 'url': observation['url']}
        state['history'].append(entry)
        state['last_result'] = result
        record_repair_use(state, repair, entry)
        source_id = state['active_document_source_id']
        latest = state.get('document_recovery', {}).get(source_id, {}).get('latest_job_id')
        if latest and state.get('document_reviews', {}).get(latest, {}).get('accepted'):
            break
    return state


def output_check(runtime, state, source_id, keys):
    latest = state.get('document_recovery', {}).get(source_id, {}).get('latest_job_id')
    if not latest:
        return {'passed': False, 'reason': 'No candidate generated'}
    _, receipt, candidate = runtime.documents._job(latest)
    rows = candidate[receipt['spec'].get('collection_key', 'records')]
    return {'passed': [r['key'] for r in rows] == keys and state['document_reviews'].get(latest, {}).get('accepted') is True,
            'keys': [r['key'] for r in rows], 'recipe': receipt['spec'], 'job_id': latest,
            'candidate_sha256': receipt['candidate_sha256']}


async def evaluate(tag):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', tag):
        raise ValueError('Use a unique simple tag')
    log = ROOT / 'logs/live-ace-documents' / tag
    output = ROOT / 'deliveries/ace-documents' / tag
    log.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    config_path = ROOT / 'examples/documents-ace-gpt-6-luna.json'
    config = load_config(str(config_path))
    config['agent']['log_dir'] = str(log / 'agent')
    config['harness'].update(state_dir=str(log / 'state'), artifact_dir=str(log / 'artifacts'))
    config['processing']['artifact_dir'] = str(log / 'processing')
    config['learning'].update(state_dir=str(log / 'playbook'), auto_replay=False)
    config['documents']['state_dir'] = str(output / 'workspace')
    training = fixture('4,4A', 'Alpha equipment', 'Keep the complete original Alpha description.')
    documents = [fixture('17,17B', 'Beta equipment', 'Keep the complete original Beta description.'),
                 fixture('28,28C', 'Gamma equipment', 'Keep the complete original Gamma description.')]
    suite = {'name': 'independent-document-labels', 'target': 'planner', 'task_type': config['learning']['task_type'],
             'host': HOST, 'evaluation': 'document_recipe', 'max_steps': 2,
             'cases': [{'task': task(d['url']), 'observation': {'url': d['url'], 'elements': []},
                        'document': d, 'initial_spec': INITIAL_SPEC, 'expected': {'keys': keys}}
                       for d, keys in zip(documents, (['17', '17B'], ['28', '28C']))]}
    suite_path = log / 'operator-replay-suite.json'
    atomic_json(suite_path, suite)
    config['learning']['replay_paths'] = [str(suite_path)]
    atomic_json(log / 'config.json', config)
    atomic_json(log / 'protocol.json', {'initial_failure': 'Operator-controlled numeric-prefix capture on a grouped source label',
        'initial_spec': INITIAL_SPEC, 'training_source': training, 'generator_repair_steps': 4,
        'replay_cases': 2, 'replay_steps_per_variant': 2,
        'admission': 'All candidate outputs pass; at least one baseline output fails. No threshold relaxation.',
        'fresh_task': 'Independent fourth source with active advice only, no current-task reflection.',
        'limitations': 'Controlled fixture mechanism validation, not unseeded task success or general performance.'})
    hashes = snapshot(ROOT, log, [Path(__file__), config_path, ROOT / 'scripts/live_document_eval_support.py'])
    receipt = {'scope': 'controlled_ace_document_loop', 'checks': {}, 'replay_reports': [], 'source_hashes': hashes}
    metrics = {}
    wire = []
    token = RUN_METRICS.set(metrics)
    try:
        config['model']['apiKey'] = os.environ[config['model']['apiKeyEnv']]
        with trace_model(log, config) as wire:
            async with ExtensionRuntime(config) as runtime:
                model = await prepare_model_options(config['model'])
                method = planner_method(config, runtime, model)
                state, opened = fresh_state(runtime, training)
                state['step'] = 1
                initial = await runtime.registry.call('document_preview', {'source_id': opened['source_id'], 'spec': INITIAL_SPEC})
                state['last_result'] = initial
                state['history'] = [{'actionId': 'A0001', 'step': 1, 'action': {'action': 'tool', 'name': 'document_preview',
                    'arguments': {'source_id': opened['source_id'], 'spec': INITIAL_SPEC}}, 'result': initial, 'url': opened['url']}]
                trigger = reflection_trigger(config, state)
                receipt['checks']['quoted_negative_review'] = bool(trigger)
                if not trigger:
                    raise RuntimeError('Controlled candidate had no supported negative review; inspect evidence')
                evidence = document_evidence(config, state, method, runtime.documents, opened['source_id'])
                reflected = await LearningService(config).reflect(evidence)
                item = {**trigger, 'method_hash': method, 'evidence_id': reflected.get('source_id'),
                        **{k: v for k, v in reflected.items() if k != 'source_id'}}
                state['repair_reflections'] = [item]
                repair = temporary_advice(config, state, method)
                receipt['checks']['separate_online_reflector'] = bool(repair)
                await generate(config, runtime, model, [], repair)
                receipt['training_output'] = output_check(runtime, state, opened['source_id'], ['4', '4A'])
                receipt['checks']['actual_repair_correct'] = receipt['training_output']['passed']
                receipt['checks']['effective_output_change'] = any(r.get('effective_change') and r.get('review_accepted') for r in state.get('repair_adoption', []))
                state['status'] = 'completed' if receipt['training_output']['passed'] else 'incomplete'
                receipt['learning'] = await LearningService(config).learn(document_evidence(config, state, method, runtime.documents, opened['source_id']), runtime=runtime)
                atomic_json(log / 'training-state.json', state)
                candidates = receipt['learning'].get('candidates', [])
                receipt['checks']['curator_created_candidate'] = bool(candidates)
                for candidate in candidates:
                    report = await LearningService(config).replay(candidate['id'], candidate['version'], suite['name'], runtime=runtime)
                    receipt['replay_reports'].append(report)
                receipt['promoted'] = any(r.get('promoted') for r in receipt['replay_reports'])
                fresh = fixture('39,39D', 'Delta equipment', 'Keep the complete original Delta description.')
                fresh_state_value, fresh_opened = fresh_state(runtime, fresh)
                fresh_state_value['last_result'] = await runtime.documents.inspect(fresh_opened['source_id'])
                with PlaybookStore(config) as store:
                    advice = store.recall(scope(config, 'planner', method, HOST), fresh_state_value['task'])
                    receipt['retained_entries'] = store.list()['entries']
                receipt['fresh_advice_ids'] = [a['id'] for a in advice]
                await generate(config, runtime, model, advice, [])
                receipt['fresh_output'] = output_check(runtime, fresh_state_value, fresh_opened['source_id'], ['39', '39D'])
                receipt['fresh_task_adoption'] = bool(advice) and receipt['fresh_output']['passed']
                atomic_json(log / 'fresh-state.json', fresh_state_value)
                receipt['checks']['source_bytes_unchanged'] = runtime.documents._source(opened['source_id'])[0] == training['content'].encode()
                receipt['checks']['replay_admission_consistent'] = all(r['promoted'] == (r['candidate_all_passed'] and any(not c['baseline']['passed'] for c in r['cases'])) for r in receipt['replay_reports'])
            receipt['model_costs'] = model_costs(wire)
    except Exception as exc:
        receipt['error'] = type(exc).__name__ + ': ' + str(exc)[:1500]
        raise
    finally:
        RUN_METRICS.reset(token)
        receipt['model_costs'] = model_costs(wire)
        receipt['metrics'] = metrics
        receipt['runtime_unchanged'] = all((ROOT / path).exists() and digest((ROOT / path).read_bytes()) == value for path, value in hashes.items())
        receipt['mechanism_checks_passed'] = bool(receipt['checks']) and all(receipt['checks'].values()) and not receipt.get('error')
        atomic_json(log / 'receipt.json', receipt)
    print(json.dumps({k: receipt[k] for k in ('mechanism_checks_passed', 'promoted', 'fresh_task_adoption', 'model_costs')}, ensure_ascii=False), flush=True)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    asyncio.run(evaluate(parser.parse_args().tag))
