import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.harness import learning
from cdp_browser_agent.harness.learning import LearningService, ReplaySuites
from cdp_browser_agent.harness.playbook import PlaybookStore, planner_method, scope
from cdp_browser_agent.harness.repair import document_evidence, reflection_trigger, temporary_advice
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.documents import review
from cdp_browser_agent.documents.engine import digest


BAD_SPEC = {'mode': 'elements', 'selector': './/Unit', 'key_source': {'attribute': 'Code'},
            'key_transforms': [{'operation': 'capture', 'pattern': '([0-9]+)'}]}
GOOD_SPEC = {'mode': 'elements', 'selector': './/Unit', 'key_source': {'attribute': 'Code'},
             'key_transforms': [{'operation': 'split', 'pattern': ','}]}


@pytest.fixture
def config(tmp_path):
    value = browser_agent_default_config()
    value['agent'].update(log_dir=str(tmp_path / 'logs'), max_steps=4, write_download_manifest=False)
    value['model'].update(model='fixture', modelDiscoveryTimeout=.01)
    value['harness'].update(state_dir=str(tmp_path / 'state'), artifact_dir=str(tmp_path / 'artifacts'))
    value['documents']['state_dir'] = str(tmp_path / 'documents')
    value['processing']['artifact_dir'] = str(tmp_path / 'processing')
    value['learning'].update(enabled=True, auto_replay=False, state_dir=str(tmp_path / 'playbook'))
    return value


def save_source(runtime, label='2,2A', body='Complete body'):
    text = f'<Catalog><Unit Code="{label}"><Body>{body}</Body></Unit></Catalog>'
    return runtime.documents._save_source(text.encode(), {'url': 'https://example.org/catalog'}, 'xml')


async def semantic_review(config, task, service, job_id, **_):
    candidate = service._job(job_id)[2]
    accepted = len(candidate['records']) > 1
    return {'accepted': accepted, 'record_unit': 'unit', 'issues': [] if accepted else [
        {'kind': 'label_mapping', 'evidence_id': 'record_0', 'quote': 'Complete body', 'problem': 'One grouped identifier was omitted'}],
        'required_changes': [] if accepted else ['Preserve every observed identifier'],
        'policy_id': review.review_policy_id(config), 'review_id': digest(job_id), 'evidence_quotes_verified': True}


def lesson(ids=('A0001',)):
    return {'trigger': 'A grouped source label lost an identifier',
            'guidance': 'Read the complete Code attribute and split it with the observed comma; keep the full Unit body for each identifier.',
            'avoid': 'Do not capture only the numeric prefix or invent labels', 'evidence_ids': list(ids)}


async def test_source_evidence_keeps_early_failure_after_long_other_source_tail(config, monkeypatch):
    monkeypatch.setattr(review, 'review_candidate', semantic_review)
    async with ExtensionRuntime(config) as runtime:
        opened = save_source(runtime)
        identity = opened['source_id']
        runtime.task_state = {'run_id': 'run', 'task': 'One object per observed identifier', 'status': 'max_steps', 'step': 1}
        result = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': BAD_SPEC})
        runtime.task_state['history'] = [{'actionId': 'A0001', 'step': 1, 'action': {'action': 'tool',
            'name': 'document_preview', 'arguments': {'source_id': identity, 'spec': BAD_SPEC}}, 'result': result}]
        runtime.task_state['history'] += [{'actionId': f'A{i:04d}', 'step': i, 'action': {'name': 'document_inspect',
            'arguments': {'source_id': 'other'}}, 'result': {'ok': True, 'source_id': 'other'}} for i in range(2, 61)]
        evidence = document_evidence(config, runtime.task_state, 'method', runtime.documents, identity)
        assert evidence['events'][0]['id'] == 'A0001'
        assert evidence['scope']['host'] == 'example.org'
        assert evidence['document']['source_unit_mapping'][0]['input'] == '2,2A'
        assert evidence['document']['source_unit_mapping'][0]['output_labels'] == ['2']
        assert evidence['document']['cited_source_evidence'][0]['text'] == 'Complete body'
        assert len(evidence['events']) == 1 and 'other' not in json.dumps(evidence)
        assert digest(runtime.documents._source(identity)[0]) in evidence['origin_hashes']


def test_reflection_trigger_deduplicates_cosmetic_changes_and_is_source_bounded(config):
    state = {'active_document_source_id': 'source', 'document_recovery': {'source': {
        'stage': 'revise', 'latest_job_id': 'first', 'output_fingerprint': 'unchanged'}},
        'document_reviews': {'first': {'accepted': False, 'issues': [{'problem': 'Observed error'}]}}}
    trigger = reflection_trigger(config, state)
    assert trigger
    state['repair_reflections'] = [{**trigger, 'status': 'completed', 'method_hash': 'm',
                                  'reflections': {'lessons': [lesson()]}}]
    state['document_recovery']['source']['latest_job_id'] = 'cosmetic'
    state['document_reviews']['cosmetic'] = deepcopy(state['document_reviews']['first'])
    assert reflection_trigger(config, state) is None
    assert temporary_advice(config, state, 'm')[0]['validated_for_future_tasks'] is False
    assert not temporary_advice(config, state, 'changed-method')
    state['active_document_source_id'] = 'another'
    assert not temporary_advice(config, state, 'm')


def test_multisource_recall_uses_active_document_before_first_task_url():
    from cdp_browser_agent.browser.agent import planner_host
    state = {'active_document_source_id': 'second', 'document_sources': {
        'second': {'url': 'https://second.example/document'}}, 'last_result': {'url': 'https://first.example/document'}}
    assert planner_host(state, {'url': 'https://first.example/document'},
                        'Read https://first.example/document and https://second.example/document') == 'second.example'
    state['pending_document_url'] = 'https://third.example/document'
    assert planner_host(state, {'url': 'https://first.example/document'}, 'Continue') == 'third.example'


async def test_repeated_inspections_trigger_before_any_candidate_and_keep_structure(config, monkeypatch):
    async with ExtensionRuntime(config) as runtime:
        opened = save_source(runtime)
        source_id = opened['source_id']
        runtime.task_state = {'run_id': 'run', 'task': 'Export all observed units', 'status': 'running', 'history': []}
        for step in range(1, 6):
            runtime.task_state['step'] = step
            result = await runtime.registry.call('document_inspect', {'source_id': source_id, 'selector': './/Unit'})
            runtime.task_state['history'].append({'actionId': f'A{step:04d}', 'step': step,
                'action': {'action': 'tool', 'name': 'document_inspect', 'arguments': {'source_id': source_id, 'selector': './/Unit'}}, 'result': result})
        trigger = reflection_trigger(config, runtime.task_state)
        assert trigger and trigger['signal'] == 'repeated_unproductive_inspection'
        assert trigger['job_id'] is None
        evidence = document_evidence(config, runtime.task_state, 'method', runtime.documents, source_id)
        assert evidence['document']['recipe'] is None
        assert evidence['events'][-1]['result']['samples'][0]['attributes']['Code'] == '2,2A'
        assert evidence['document']['review'] == {}


async def test_online_reflection_does_not_create_or_activate_retained_advice(config, monkeypatch):
    evidence = {'source': {'run_id': 'run'}, 'scope': scope(config, 'planner', 'method', 'example.org'),
                'origin_hashes': ['original'], 'events': [{'id': 'A0001', 'result': {'ok': False}}]}
    calls = AsyncMock(return_value=json.dumps({'lessons': [lesson()]}))
    monkeypatch.setattr(learning, 'chat_completion', calls)
    result = await LearningService(config).reflect(evidence)
    assert result['status'] == 'completed'
    assert await LearningService(config).reflect(evidence) == result
    assert calls.await_count == 1
    with PlaybookStore(config) as store:
        assert not store.list()['entries'] and not store.recall(evidence['scope'])
    calls.return_value = json.dumps({'lessons': [lesson(('invented',))]})
    failed = await LearningService(config).reflect({**evidence, 'source': {'run_id': 'different'}})
    assert failed['status'] == 'failed' and 'nonexistent' in failed['error']


async def test_running_agent_consumes_reflection_before_repair_and_curates_afterward(config, monkeypatch):
    from cdp_browser_agent.browser import agent
    from cdp_browser_agent.harness.session import RunSession
    monkeypatch.setattr(agent, 'prepare_model_options', AsyncMock(side_effect=lambda model: model))
    monkeypatch.setattr(review, 'review_candidate', semantic_review)
    # First call is online Reflector, then the terminal Reflector and Curator.
    calls = AsyncMock(side_effect=[json.dumps({'lessons': [lesson()]}), json.dumps({'lessons': [lesson()]}),
                                  json.dumps({'operations': [{'type': 'ADD', 'lesson': lesson()}]})])
    monkeypatch.setattr(learning, 'chat_completion', calls)
    async with ExtensionRuntime(config) as runtime:
        opened = save_source(runtime)
        session = RunSession(config, 'One object per identifier in https://example.org/catalog')
        session.state['document_sources'] = {opened['source_id']: opened}
        planned = []
        async def planner(request):
            planned.append(deepcopy(request['memory_context']))
            step = request['step']
            if step <= 2:
                if step == 2:
                    assert request['memory_context']['repair_advice'][0]['guidance'] == lesson()['guidance']
                    assert not request['memory_context']['playbook_advice']
                return {'action': {'action': 'tool', 'name': 'document_preview', 'arguments': {
                    'source_id': opened['source_id'], 'spec': BAD_SPEC if step == 1 else GOOD_SPEC}}}
            return {'action': {'action': 'done', 'outcome': 'completed', 'answer': 'Recipe corrected'}}
        monkeypatch.setattr(agent, 'plan_next_action', planner)
        try:
            state = await agent.run_agent(session.state['task'], config, runtime, session=session)
        finally:
            session.finish()
        assert state['status'] == 'completed'
        assert state['repair_reflections'][0]['source_id'] == opened['source_id']
        assert state['repair_reflections'][0]['evidence_id'] != opened['source_id']
        assert state['repair_adoption'][0]['effective_change'] is True
        assert state['repair_adoption'][0]['review_accepted'] is True
        assert calls.await_count == 3
        with PlaybookStore(config) as store:
            assert store.list()['entries'][0]['state'] == 'candidate'
            assert not store.recall(store.list()['entries'][0]['scope'])


async def test_transient_review_retry_preserves_old_attempt_and_same_candidate(config, monkeypatch):
    response = httpx.Response(408, request=httpx.Request('POST', 'http://localhost/v1/responses'))
    calls = AsyncMock(side_effect=[httpx.HTTPStatusError('Disconnected stream', request=response.request, response=response),
        json.dumps({'accepted': True, 'record_unit': 'unit', 'issues': [], 'required_changes': [],
            'sample_checks': [{'evidence_id': 'record_0', 'unit_matches': True, 'body_matches': True, 'reason': 'Full unit body'}],
            'coverage_checks': []})])
    monkeypatch.setattr(review, 'chat_completion', calls)
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state.update(task='One object per source unit', run_id='run')
        identity = save_source(runtime, '2')['source_id']
        first = await runtime.registry.call('document_preview', {'source_id': identity,
            'spec': {'mode': 'elements', 'selector': './/Unit'}})
        assert first['review']['retryable'] and first['review']['issues'] == []
        assert runtime.task_state['document_recovery'][identity]['stage'] == 'review_pending'
        assert reflection_trigger(config, runtime.task_state) is None
        folder, receipt, _ = runtime.documents._job(first['job_id'])
        old_path = folder / 'reviews' / (first['review']['review_id'][:32] + '.json')
        old = old_path.read_bytes()
        second = await runtime.registry.call('document_review_retry', {'job_id': first['job_id'],
            'expected_review_id': first['review']['review_id']})
        assert second['review']['accepted'] and second['review']['review_attempt'] == 2
        assert old_path.read_bytes() == old and second['review']['candidate_sha256'] == receipt['candidate_sha256']
        duplicate = await runtime.registry.call('document_review_retry', {'job_id': first['job_id'],
            'expected_review_id': first['review']['review_id']})
        assert not duplicate['ok'] and calls.await_count == 2
        assert (await runtime.registry.call('document_export', {'job_id': first['job_id']}))['ok']


async def test_executed_document_replay_keeps_gold_hidden_and_admits_only_real_output_gain(config, monkeypatch, tmp_path):
    from cdp_browser_agent.browser import planner
    monkeypatch.setattr(review, 'review_candidate', semantic_review)
    observed = []
    async def generate(request):
        observed.append(deepcopy(request))
        focus = request['extensions']['document_focus']
        return {'action': {'action': 'tool', 'name': 'document_preview', 'arguments': {
            'source_id': focus['active_source_id'], 'spec': GOOD_SPEC if request['memory_context']['playbook_advice'] else BAD_SPEC}}}
    monkeypatch.setattr(planner, 'plan_next_action', generate)
    cases = [{'task': f'Export batch {n} as one object per identifier', 'observation': {'url': 'https://example.org/catalog', 'elements': []},
        'document': {'content': f'<Catalog><Unit Code="{n},{n}A"><Body>Complete body</Body></Unit></Catalog>',
                     'format': 'xml', 'url': 'https://example.org/catalog'}, 'initial_spec': BAD_SPEC,
        'expected': {'keys': [str(n), str(n) + 'A'], 'text_by_key': {str(n): 'Complete body', str(n) + 'A': 'Complete body'}}}
        for n in (7, 9)]
    suite = {'name': 'grouped-labels', 'target': 'planner', 'task_type': 'general', 'host': 'example.org',
             'evaluation': 'document_recipe', 'max_steps': 2, 'cases': cases}
    path = tmp_path / 'suite.json'
    path.write_text(json.dumps(suite))
    config['learning']['replay_paths'] = [str(path)]
    async with ExtensionRuntime(config) as runtime:
        method = planner_method(config, runtime, await learning.prepare_model_options(config['model']))
        with PlaybookStore(config) as store:
            source = store.save_evidence({'scope': scope(config, 'planner', method, 'example.org'),
                'events': [{'id': 'A0001'}], 'origin_hashes': ['unseen-training-input']})
            entry = store.propose(lesson(), source)
        original_state = runtime.task_state
        report = await LearningService(config).replay(entry['id'], entry['version'], 'grouped-labels', runtime=runtime)
        assert report['scope'] == 'document_recipe_outputs' and report['promoted']
        assert all(not c['baseline']['passed'] and c['candidate']['passed'] for c in report['cases'])
        assert runtime.task_state is original_state
        assert all('expected' not in r and 'cases' not in r for r in observed)
        with PlaybookStore(config) as store:
            assert store.recall(entry['scope'])[0]['id'] == entry['id']
        # Same source bytes with another URL/task are still a training overlap.
        with PlaybookStore(config) as store:
            source = store.save_evidence({'scope': entry['scope'], 'events': [{'id': 'A0001'}],
                'origin_hashes': [ReplaySuites.input_hash(suite, cases[0])]})
            other = store.propose({**lesson(), 'guidance': 'Different candidate'}, source)
        with pytest.raises(ValueError, match='overlap'):
            await LearningService(config).replay(other['id'], 1, 'grouped-labels', runtime=runtime)
