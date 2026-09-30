import json
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.documents.engine import DocumentTools, digest
from cdp_browser_agent.documents.recovery import DocumentRecovery
from cdp_browser_agent.documents.context import project_context
from cdp_browser_agent.documents.review import build_evidence, validate_decision
from cdp_browser_agent.harness.artifacts import ArtifactStore
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.workflows.store import atomic_json


@pytest.fixture
def config(tmp_path):
    value = browser_agent_default_config()
    value['documents']['state_dir'] = str(tmp_path / 'documents')
    value['harness']['artifact_dir'] = str(tmp_path / 'artifacts')
    return value


def source(service, text, kind='xml'):
    return service._save_source(text.encode(), {'url': 'https://example.org/manual'}, kind)['source_id']


async def test_source_attribute_list_range_pipeline_preserves_full_units_and_replays(config):
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Unit Code="10:12"><Name>Retired units</Name><Body>Original marker</Body></Unit>'
        '<Unit Code="17,17A"><Name>Shared variant</Name><Body>Unchanged specification</Body></Unit><Table>Price list</Table></Catalog>')
    spec = {'mode': 'elements', 'selector': './/Unit', 'title_selector': 'Name',
        'key_source': {'attribute': 'Code'}, 'key_transforms': [
            {'operation': 'split', 'pattern': ','}, {'operation': 'integer_range', 'delimiter': ':', 'max_values': 20}]}
    preview = await service.preview(identity, spec)
    _, receipt, candidate = service._job(preview['job_id'])
    assert [r['key'] for r in candidate['records']] == ['10', '11', '12', '17', '17A']
    assert len({r['id'] for r in candidate['records']}) == 5
    assert all(r['shared_source'] for r in candidate['records'])
    assert candidate['records'][0]['text'] == 'Retired unitsOriginal marker'
    assert candidate['source_unit_mapping'][0]['input'] == '10:12'
    assert candidate['source_unit_mapping'][0]['steps'][-1]['output'] == ['10', '11', '12']
    assert candidate['source_unit_mapping'][0]['record_ids'] == [r['id'] for r in candidate['records'][:3]]
    assert candidate['coverage']['tree_conserved'] and candidate['coverage']['remainder_tags']['Table'] == 1
    exported = await service.export(preview['job_id'])
    assert (await DocumentTools(config).export(preview['job_id']))['output_sha256'] == exported['output_sha256']
    assert receipt['engine'] == 2


async def test_label_child_capture_split_is_explicit_and_does_not_return_delimiter_capture(config):
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Unit><Name>Models 3, 3A</Name><Body>One original body</Body></Unit></Catalog>')
    preview = await service.preview(identity, {'mode': 'elements', 'selector': './/Unit',
        'key_source': {'selector': 'Name'}, 'key_transforms': [{'operation': 'capture', 'pattern': 'Models (.+)'},
            {'operation': 'split', 'pattern': '(,)'}]})
    candidate = service._job(preview['job_id'])[2]
    assert [r['key'] for r in candidate['records']] == ['3', '3A']
    assert candidate['source_unit_mapping'][0]['source_path'].endswith('/Name[1]')


@pytest.mark.parametrize('value', ['12:10', 'A:12', '1:1000000000'])
async def test_invalid_or_unbounded_ranges_fail_without_changing_source(config, value):
    service = DocumentTools(config)
    identity = source(service, f'<Catalog><Unit Code="{value}">Original</Unit></Catalog>')
    raw = service._source(identity)[0]
    with pytest.raises(ValueError, match='range|integers'):
        await service.preview(identity, {'mode': 'elements', 'selector': './/Unit', 'key_source': {'attribute': 'Code'},
            'key_transforms': [{'operation': 'integer_range', 'delimiter': ':', 'max_values': 20}]})
    assert service._source(identity)[0] == raw
    assert not (service.root / 'jobs').exists()


async def test_original_engine_job_remains_byte_equivalent_after_new_pipeline(config):
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Unit>Original</Unit></Catalog>')
    spec = {'mode': 'elements', 'selector': './/Unit'}
    candidate = service._transform(identity, spec, 1)
    job_id = digest({'source_id': identity, 'spec': spec, 'engine': 1})
    folder = service._path(job_id, 'jobs')
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / 'candidate.json', candidate)
    atomic_json(folder / 'receipt.json', {'source_id': identity, 'spec': spec, 'engine': 1, 'candidate_sha256': digest(candidate)})
    exported = await service.export(job_id)
    assert json.loads((folder / 'export.json').read_text(encoding='utf-8')) == candidate
    assert 'source_unit_mapping' not in candidate
    new = await service.preview(identity, spec)
    assert new['job_id'] != job_id and (await service.export(new['job_id']))['replay_matches']
    assert exported['replay_matches']


async def test_capture_split_diagnostic_must_be_assessed_before_acceptance(config):
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Unit><Name>Models 3,3A</Name><Body>Original body</Body></Unit></Catalog>')
    preview = await service.preview(identity, {'mode': 'elements', 'selector': './/Unit', 'key_source': {'selector': 'Name'},
        'key_transforms': [{'operation': 'capture', 'pattern': 'Models ([0-9]+)'}, {'operation': 'split', 'pattern': ','}]})
    _, receipt, candidate = service._job(preview['job_id'])
    evidence = build_evidence(service, receipt, candidate)
    assert evidence['diagnostic_evidence_ids'] == ['key_diagnostic_0']
    assert evidence['source_unit_mapping']['samples'][0]['output_labels'] == ['3']
    decision = {'accepted': True, 'record_unit': 'model', 'issues': [], 'required_changes': [],
        'sample_checks': [{'evidence_id': 'record_0', 'unit_matches': True, 'body_matches': True, 'reason': 'Source body preserved'}],
        'coverage_checks': []}
    with pytest.raises(ValueError, match='diagnostic_checks'):
        validate_decision(json.dumps(decision), evidence['evidence'])
    decision['diagnostic_checks'] = [{'evidence_id': 'key_diagnostic_0', 'resolved': False, 'quote': ',3A', 'reason': 'A model label was omitted'}]
    with pytest.raises(ValueError, match='Unresolved'):
        validate_decision(json.dumps(decision), evidence['evidence'])
    decision['diagnostic_checks'][0].update(resolved=True, quote='fabricated')
    with pytest.raises(ValueError, match='verified quote'):
        validate_decision(json.dumps(decision), evidence['evidence'])


async def test_review_mapping_views_bound_long_source_values_without_truncating_candidate(config):
    from cdp_browser_agent.context_budget import ContextBudget
    from cdp_browser_agent.documents.review import SYSTEM
    service = DocumentTools(config)
    body = 'Full original entry and supporting details. ' * 4000
    identity = source(service, '<Catalog>' + ''.join(f'<Unit>{i}: {body}</Unit>' for i in range(4)) + '</Catalog>')
    preview = await service.preview(identity, {'mode': 'elements', 'selector': './/Unit'})
    _, receipt, candidate = service._job(preview['job_id'])
    original = digest(candidate)
    projection = build_evidence(service, receipt, candidate)
    mapping = projection['source_unit_mapping']['samples'][0]
    assert mapping['input_preview_truncated'] and mapping['input_chars'] > 100000
    assert len(mapping['input']) <= 1000 and mapping['full_mapping_reference']['index'] == 0
    payload = {'source_overview': await service.inspect(identity), 'recipe': receipt['spec'],
        'coverage': candidate['coverage'], **projection}
    ContextBudget.from_settings(config['model'], config['agent']).check([
        {'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': json.dumps(payload)}])
    assert digest(candidate) == original and candidate['records'][0]['text'] == '0: ' + body.strip()
    assert len(candidate['source_unit_mapping'][0]['input']) > 100000


async def test_review_mapping_samples_keep_correct_labels_and_record_ids_for_large_group(config):
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Unit Code="1:1000">Original grouped marker</Unit></Catalog>')
    preview = await service.preview(identity, {'mode': 'elements', 'selector': './/Unit',
        'key_source': {'attribute': 'Code'}, 'key_transforms': [{'operation': 'integer_range', 'delimiter': ':', 'max_values': 1000}]})
    _, receipt, candidate = service._job(preview['job_id'])
    projection = build_evidence(service, receipt, candidate)
    mapping = projection['source_unit_mapping']['samples'][0]
    assert mapping['output_label_count'] == mapping['record_count'] == 1000
    assert len(mapping['record_indexes']) == len(mapping['output_labels']) == len(mapping['record_ids']) <= 8
    assert mapping['steps'][0]['output_count'] == 1000 and mapping['steps'][0]['output_preview_truncated']
    for i, label, record_id in zip(mapping['record_indexes'], mapping['output_labels'], mapping['record_ids']):
        assert candidate['records'][i]['key'] == label and candidate['records'][i]['id'] == record_id


async def test_repair_guard_persists_counts_ignores_metadata_and_allows_other_source(config, monkeypatch):
    from cdp_browser_agent.documents import review
    calls = AsyncMock(return_value=json.dumps({'accepted': False, 'record_unit': 'product',
        'issues': [{'kind': 'record_unit', 'evidence_id': 'record_0', 'quote': 'Overview', 'problem': 'Overview is not a product'}],
        'required_changes': ['Select actual products']}))
    monkeypatch.setattr(review, 'chat_completion', calls)
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state['task'] = 'Export one product per object.'
        identity = source(runtime.documents, '<p>Overview</p><p class="product">A</p>', 'html')
        spec = {'mode': 'elements', 'selector': 'p'}
        first = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': spec})
        second = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': {**spec, 'metadata': {'note': 'different'}}})
        assert not second['revision']['effective_change']
        assert second['revision']['changed_spec_fields'] == []
        assert calls.await_count == 1
        checkpoint = json.loads(json.dumps(runtime.task_state))
        runtime.task_state = checkpoint
        blocked = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': spec})
        assert not blocked['ok'] and blocked['effects'].endswith('not executed.')
        for _ in range(8):
            await runtime.registry.call('document_review', {'job_id': first['job_id']})
        assert runtime.task_state['document_recovery'][identity]['stage'] == 'blocked'
        assert not (await runtime.registry.call('document_preview', {'source_id': identity, 'spec': {'mode': 'elements', 'selector': 'p.product'}}))['ok']
        other = source(runtime.documents, '<p>Another source</p>', 'html')
        assert (await runtime.registry.call('document_inspect', {'source_id': other}))['ok']
        assert runtime.task_state['document_recovery'][other]['stage'] == 'inspect'


async def test_effective_body_revision_resets_diagnosis_and_records_actual_changes(config):
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Unit><Name>A</Name><Body>Important body</Body></Unit></Catalog>')
    state = {}
    recovery = DocumentRecovery(state, service, config['documents'])
    first = await service.preview(identity, {'mode': 'elements', 'selector': './/Unit', 'body_selector': 'Name'})
    recovery.candidate(first)
    for _ in range(4):
        recovery.failure({'source_id': identity}, 'No effective correction')
    assert recovery.ledger(identity)['stage'] == 'diagnose'
    second = await service.preview(identity, {'mode': 'elements', 'selector': './/Unit'})
    change = recovery.candidate(second)
    assert change['effective_change'] and change['changed_spec_fields'] == ['body_selector']
    assert {'text', 'paragraphs'} <= set(change['changed_record_fields'])
    assert recovery.ledger(identity)['stage'] == 'review'
    assert recovery.ledger(identity)['unproductive_actions'] == 0


def test_source_focus_reduces_history_preserves_current_defects_and_restoration(tmp_path):
    store = ArtifactStore(tmp_path / 'artifacts')
    active, other = 'a' * 64, 'b' * 64
    payload = {'task': 'Export both manuals', 'run_notes': {'decisions': ['Keep original wording']},
        'extensions': {'document_focus': {'active_source_id': active, 'active_candidate': {'job_id': 'c' * 64,
            'review': {'accepted': False, 'issues': [{'quote': 'Missing step'}]}}, 'recipe': {'selector': './/Unit'}},
            'document_sources': [{'source_id': active, 'url': 'https://example.org/a'}]},
        'last_result': {'status': 'needs_revision', 'quote': 'Missing step'},
        'recent_history': [{'action_id': f'A{i:04d}', 'arguments': {'source_id': other}, 'text': 'Past output ' * 2000} for i in range(30)] +
            [{'action_id': 'A0030', 'arguments': {'source_id': active}, 'text': 'Current observed structure'}],
        'recalled_relevant_history': [], 'task_memory': {'old': 'Large old state ' * 5000},
        'run_memory_brief': {'old': 'Large old summary'}, 'sources': [], 'compressed_action_memory': []}
    original = deepcopy(payload)
    view, receipt = project_context(payload, store, {}, {'previous_plan_seconds': 70, 'remaining_seconds': 50, 'remaining_steps': 5})
    assert receipt['after_chars'] < receipt['before_chars'] / 3
    assert receipt['target_prompt_tokens'] == 10000
    assert view['task'] == original['task'] and view['run_notes'] == original['run_notes']
    assert view['extensions'] == original['extensions'] and view['last_result'] == original['last_result']
    assert any(r['action_id'] == 'A0030' for r in view['recent_history'])
    assert store.load(view['context_archive']['artifact_id']) == original
    assert payload == original


async def test_review_deadline_keeps_candidate_recipe_and_does_not_invent_defects(config, monkeypatch):
    from cdp_browser_agent.documents import review
    async def slow(*_):
        await asyncio.sleep(30)
    monkeypatch.setattr(review, 'chat_completion', slow)
    config['documents']['review_timeout_seconds'] = .02
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state['task'] = 'One object per device.'
        identity = source(runtime.documents, '<Catalog><Unit>Source device</Unit></Catalog>')
        spec = {'mode': 'elements', 'selector': './/Unit'}
        result = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': spec})
        assert result['ok'] and result['status'] == 'review_inconclusive'
        assert result['review']['issues'] == [] and not result['review']['evidence_quotes_verified']
        focus = runtime.context()['document_focus']
        assert focus['recipe'] == spec and focus['active_candidate']['job_id'] == result['job_id']
        assert not (await runtime.registry.call('document_export', {'job_id': result['job_id']}))['ok']


async def test_grouped_unchanged_records_do_not_claim_label_differences(config):
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Unit Code="2,2A">Shared specification</Unit></Catalog>')
    spec = {'mode': 'elements', 'selector': './/Unit', 'key_source': {'attribute': 'Code'},
        'key_transforms': [{'operation': 'split', 'pattern': ','}]}
    state = {}
    recovery = DocumentRecovery(state, service, config['documents'])
    first = await service.preview(identity, spec)
    recovery.candidate(first)
    second = await service.preview(identity, {**spec, 'metadata': {'description': 'New description'}})
    change = recovery.candidate(second)
    assert not change['effective_change'] and change['changed_record_fields'] == []
    assert change['changed_source_paths'] == [] and change['changed_spec_fields'] == []


async def test_reopening_saved_source_or_redirect_reuses_verified_identity_without_fetch(config, tmp_path):
    original = tmp_path / 'source.xml'
    original.write_bytes(b'<Catalog><Unit>Full original body</Unit></Catalog>')
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state['task'] = 'Collect one unit per object'
        runtime.web.fetch = AsyncMock(return_value={'ok': True, 'artifact_paths': ['', str(original)],
            'response_sha256': digest(original.read_bytes()), 'requested_url': 'https://example.org/start',
            'url': 'https://example.org/final', 'content_type': 'application/xml'})
        first = await runtime.registry.call('document_open', {'url': 'https://example.org/start'})
        cached = await runtime.registry.call('document_open', {'url': 'https://example.org/final#top'})
        assert cached['status'] == 'source_already_saved' and cached['source_id'] == first['source_id']
        assert runtime.task_state['active_document_source_id'] == first['source_id']
        assert 'pending_document_url' not in runtime.task_state
        runtime.web.fetch.assert_awaited_once()
        saved = runtime.documents._path(first['source_id'], 'sources') / 'source.bin'
        saved.write_bytes(b'changed')
        rejected = await runtime.registry.call('document_open', {'url': 'https://example.org/start'})
        assert not rejected['ok'] and 'hash changed' in rejected['message']
        runtime.web.fetch.assert_awaited_once()


async def test_failed_retrieval_clears_old_source_focus_and_preserves_pending_context(config, tmp_path):
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state.update(task='Collect both sources', active_document_source_id='a' * 64)
        runtime.web.fetch = AsyncMock(return_value={'ok': False, 'url': 'https://example.org/new',
            'status': 'network_error', 'message': 'Transport unavailable'})
        result = await runtime.registry.call('document_open', {'url': 'https://example.org/new'})
        assert not result['ok'] and runtime.task_state['active_document_source_id'] is None
        focus = runtime.context()['document_focus']
        assert focus['pending_url'] == 'https://example.org/new' and focus['retrieval']['attempts'] == 1
        payload = {'task': 'Collect both sources', 'extensions': runtime.context(), 'sources': [],
            'recent_history': [{'action_id': 'A0001', 'url': 'https://example.org/old'},
                               {'action_id': 'A0002', 'arguments': {'url': 'https://example.org/new'}}],
            'last_result': result, 'task_memory': {}}
        view, receipt = project_context(payload, ArtifactStore(tmp_path / 'projection'), {}, {'remaining_seconds': 30})
        assert receipt['status'] == 'source_focused' and view['task_memory']['stage'] == 'access_gap'
        assert view['task_memory']['pending_url'] == 'https://example.org/new'
        assert view['last_result'] == result


def test_retrieval_attempt_budget_persists_across_http_browser_and_redirects(config):
    service = DocumentTools(config)
    recovery = DocumentRecovery({}, service, config['documents'])
    start, final = 'https://example.org/start', 'https://example.org/final'
    assert recovery.before('document_open', {'url': start}) is None
    recovery.after('document_open', {'url': start}, {'ok': False, 'url': final, 'status': 'http_error', 'http_status': 503})
    state = json.loads(json.dumps(recovery.state))
    recovery = DocumentRecovery(state, service, config['documents'])
    assert recovery.before_access('download', final) is None
    recovery.after_access('download', final, {'ok': False, 'http_status': 503})
    assert recovery.before_access('download', start) is None
    recovery.after_access('download', start, {'ok': False, 'http_status': 503})
    denied = recovery.before('document_open', {'url': final})
    assert not denied['ok'] and denied['retrieval']['attempts'] == 3
    assert denied['retrieval']['operations'] == {'document_open': 1, 'download': 2}
    assert recovery.before('document_open', {'url': 'https://example.org/other'}) is None


def test_unsupported_representation_and_rate_limit_require_changed_evidence_or_wait(config, monkeypatch):
    import cdp_browser_agent.documents.recovery as module
    monkeypatch.setattr(module.time, 'time', lambda: 1000)
    recovery = DocumentRecovery({}, DocumentTools(config), config['documents'])
    unsupported = 'https://example.org/binary'
    assert recovery.before('document_open', {'url': unsupported}) is None
    recovery.after('document_open', {'url': unsupported}, {'ok': False, 'status': 'unsupported_content', 'http_status': 200})
    assert recovery.before('document_open', {'url': unsupported})['errorType'] == 'document_recovery_guard'
    limited = 'https://example.org/feed'
    assert recovery.before('document_open', {'url': limited}) is None
    recovery.after('document_open', {'url': limited}, {'ok': False, 'status': 'rate_limited', 'http_status': 429, 'retry_after': '60'})
    denied = recovery.before_access('download', limited)
    assert denied['next_retry_at'] == 1060 and denied['retrieval']['attempts'] == 1
    monkeypatch.setattr(module.time, 'time', lambda: 1061)
    assert recovery.before_access('download', limited) is None
    recovery.after_access('download', limited, {'ok': False, 'http_status': 429, 'retry_after': 'not-a-date'})
    assert recovery.target(limited)['next_retry_at'] == 1121
