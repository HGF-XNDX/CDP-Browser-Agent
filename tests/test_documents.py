import base64
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.documents.engine import DocumentTools, digest
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.harness.verification import check_documents
from cdp_browser_agent.mcp_server import create_mcp_server


@pytest.fixture
def config(tmp_path):
    value = browser_agent_default_config()
    value['documents']['state_dir'] = str(tmp_path / 'documents')
    value['web']['artifact_dir'] = str(tmp_path / 'web')
    value['harness']['artifact_dir'] = str(tmp_path / 'artifacts')
    return value


def source(service, text, kind='html'):
    return service._save_source(text.encode(), {'url': 'https://example.org/specification', 'content_type': 'text/' + kind}, kind)['source_id']


async def test_section_recipe_preserves_inline_words_notes_and_unselected_table(config):
    service = DocumentTools(config)
    identity = source(service, '<html><h1>Device manual</h1><p>Introduction</p><h2 class="part">Unit A</h2>'
        '<p class="body">oper<!-- pagebreak -->ations with <b>bold</b> text</p><p class="note">Revision note</p>'
        '<h2 class="part">Unit B</h2><p class="body">Second unit</p><table><tr><td>Prices</td></tr></table></html>')
    preview = await service.preview(identity, {'mode': 'sections', 'selector': 'h2.part, p',
        'heading_selector': 'h2.part', 'body_selector': 'p.body', 'key_pattern': 'Unit (.+)'})
    assert preview['record_count'] == 2
    assert preview['coverage']['unassigned_selected_units'] == 1
    assert preview['coverage']['remainder_tags']['table'] == 1
    result = await service.export(preview['job_id'])
    folder, receipt, output = service._job(preview['job_id'])
    assert output['records'][0]['text'] == 'Unit A\noperations with bold text'
    assert output['records'][0]['annotations'][0]['text'] == 'Revision note'
    assert output['coverage']['tree_conserved']
    assert output['coverage']['semantic_completeness'] == 'requires_review'
    assert output['metadata']['origin'] == 'planner_supplied'
    again = await DocumentTools(config).export(preview['job_id'])
    assert again['output_sha256'] == result['output_sha256']
    # Editing any published file is detected, never silently "repaired".
    (folder / 'export.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='Existing export changed'):
        await service.export(preview['job_id'])


async def test_runtime_rule_change_and_grouped_keys_are_frozen(config):
    service = DocumentTools(config)
    identity = source(service, '<p>Section 1, 1a. Shared notice</p><p>Detail</p><p>Chapter Two</p>'
        '<p>Chapter introduction</p><p>Section 2. Next</p><p>Continuation</p>')
    spec = {'mode': 'sections', 'selector': 'p', 'start_pattern': '^Section', 'key_pattern': r'^Section ([^.]+)',
        'key_separator': r',\s*', 'collection_key': 'items'}
    first = await service.preview(identity, spec)
    spec['exclude_pattern'] = '^Chapter'
    second = await service.preview(identity, spec)
    assert first['job_id'] != second['job_id']
    assert second['record_count'] == 3
    rows = service._job(second['job_id'])[2]['items']
    assert [r['key'] for r in rows] == ['1', '1a', '2']
    assert rows[0]['shared_source'] and rows[1]['shared_source']
    assert 'Chapter' not in rows[0]['text']


async def test_encoded_xml_with_repeated_labels_tail_items_and_appendix(config):
    service = DocumentTools(config)
    xml = '<Catalog><Title>Parts</Title><Series><Part n="1"><Name>Item 1</Name><Body>A<Sub>B</Sub>C</Body></Part></Series>' \
          '<Series><Part n="1"><Name>Item 1</Name><Body>D</Body></Part><P>Unnumbered material</P></Series>' \
          '<Appendix><Table><Row><Cell>Keep fee table</Cell></Row></Table></Appendix></Catalog>'
    parent = source(service, json.dumps({'edition': '2026-01', 'payload': base64.b64encode(xml.encode()).decode()}), 'json')
    inspection = await service.inspect(parent)
    assert inspection['value']['edition'] == '2026-01'
    child = await service.decode(parent, '/payload')
    samples = await service.inspect(child['source_id'], './/Part')
    assert samples['total'] == 2
    paged = await service.inspect(child['source_id'], './/Part', limit=1)
    assert paged['next_page_arguments']['selector'] == './/Part'
    assert (await service.inspect(**paged['next_page_arguments']))['samples'][0]['text'].endswith('D')
    assert (await service.inspect(child['source_id'], offset=10))['status'] == 'offset_out_of_range'
    assert (await service.inspect(child['source_id'], '//Part'))['total'] == 2
    assert (await service.inspect(child['source_id'], '/Catalog/Series/Part'))['total'] == 2
    assert (await service.inspect(child['source_id'], '//Catalog/Series/Part'))['total'] == 2
    assert (await service.inspect(child['source_id'], 'part'))['status'] == 'empty_selection'
    preview = await service.preview(child['source_id'], {'mode': 'elements', 'selector': './/Part',
        'title_selector': 'Name', 'body_selector': 'Body'})
    assert preview['coverage']['duplicate_keys'] == ['Item 1']
    assert preview['coverage']['remainder_tags']['Appendix'] == 1
    output = service._job(preview['job_id'])[2]
    assert len({r['id'] for r in output['records']}) == 2
    assert output['records'][0]['text'] == 'ABC'
    assert 'Unnumbered material' in json.dumps(output['remainder'])
    result = await service.export(preview['job_id'])
    assert result['replay_matches']
    (service._path(parent, 'sources') / 'source.bin').write_text('{}')
    with pytest.raises(ValueError, match='source/hash changed'):
        await service.export(preview['job_id'])


async def test_reject_overlaps_empty_matches_entity_and_path_escape(config):
    service = DocumentTools(config)
    identity = source(service, '<div><p>Message</p></div>')
    with pytest.raises(ValueError, match='overlapping'):
        await service.preview(identity, {'mode': 'elements', 'selector': 'div, p'})
    with pytest.raises(ValueError, match='no nodes'):
        await service.preview(identity, {'mode': 'elements', 'selector': '.missing'})
    with pytest.raises(ValueError, match='Invalid document identity'):
        await service.inspect('../private')
    xml = source(service, '<!DOCTYPE x [<!ENTITY a "explosion">]><x>&a;</x>', 'xml')
    with pytest.raises(ValueError, match='DTD/entity'):
        await service.inspect(xml)
    with pytest.raises(Exception):
        await service.preview(identity, {'mode': 'elements', 'selector': 'p', 'code': 'print(1)'})


async def test_bad_regex_and_record_budget_fail_closed(config):
    service = DocumentTools(config)
    identity = source(service, '<p>' + 'a' * 30000 + '!</p>')
    with pytest.raises(TimeoutError):
        await service.preview(identity, {'mode': 'sections', 'selector': 'p', 'start_pattern': '(a+)+$'})
    config['documents']['max_records'] = 1
    small = DocumentTools(config)
    identity = source(small, '<p>One</p><p>Two</p>')
    with pytest.raises(ValueError, match='record limit'):
        await small.preview(identity, {'mode': 'elements', 'selector': 'p'})


async def test_declared_key_constraint_blocks_structurally_complete_wrong_split(config):
    service = DocumentTools(config)
    identity = source(service, '<h2 class="group">Catalog</h2><h2 class="item">SKU 1</h2><p>Product details</p>')
    with pytest.raises(ValueError, match='absent from selector'):
        await service.preview(identity, {'mode': 'sections', 'selector': 'p', 'heading_selector': 'h2.item'})
    preview = await service.preview(identity, {'mode': 'sections', 'selector': 'h2, p',
        'heading_selector': 'h2', 'key_pattern': r'^SKU (\d+)'})
    assert preview['coverage']['tree_conserved']
    assert not preview['validation']['ok']
    assert preview['validation']['unmatched_key_count'] == 1
    with pytest.raises(ValueError, match='Recipe constraints failed'):
        await service.export(preview['job_id'])
    corrected = await service.preview(identity, {'mode': 'sections', 'selector': 'h2.item, p',
        'heading_selector': 'h2.item', 'key_pattern': r'^SKU (\d+)'})
    assert corrected['validation']['ok']
    exported = await service.export(corrected['job_id'])
    rows = DocumentTools(config).records(corrected['job_id'])
    assert len(rows) == 1 and rows[0]['data']['text'] == 'SKU 1\nProduct details'
    assert rows[0]['data']['document_source_id'] == identity
    info = await service.inspect(identity)
    assert info['observed_selectors']['h2.item'] == 1


async def test_key_split_diagnostics_show_uncaptured_delimiter_and_prioritize_review(config):
    from cdp_browser_agent.documents.review import build_evidence
    service = DocumentTools(config)
    markup = ''.join(f'<h2 class="entry">Unit {i}' + (',18A' if i == 18 else '') +
        f'</h2><p class="body" style="margin-left:{min(i, 6)}px">Body {i}</p>' for i in range(30))
    identity = source(service, markup)
    preview = await service.preview(identity, {'mode': 'sections', 'selector': 'h2.entry,p.body',
        'heading_selector': 'h2.entry', 'body_selector': 'p.body', 'key_pattern': '^Unit ([0-9]+)', 'key_separator': ','})
    # A separator cannot split identifiers that the capture already omitted.
    assert preview['validation']['ok']  # This diagnostic is not a semantic verdict.
    warning = preview['key_diagnostics']['warnings'][0]
    assert warning['record_index'] == 18 and warning['extracted_key'] == '18'
    assert warning['unconsumed_suffix'].startswith(',18A')
    _, receipt, candidate = service._job(preview['job_id'])
    projection = build_evidence(service, receipt, candidate)
    assert 18 in {sample['index'] for sample in projection['samples']}
    assert len(projection['samples']) <= 7
    assert projection['output_shape']['record_identity']['id_is_unique']
    assert not projection['output_shape']['record_identity']['label_key_must_be_unique']


async def test_grouped_records_respect_materialized_text_budget_without_losing_source(config):
    config['documents']['max_materialized_chars'] = 3000
    service = DocumentTools(config)
    identity = source(service, '<h2>A,B,C,D</h2><p>' + 'x' * 1200 + '</p>')
    raw_before, _ = service._source(identity)
    spec = {'mode': 'sections', 'selector': 'h2,p', 'heading_selector': 'h2', 'key_separator': ','}
    with pytest.raises(ValueError, match='materialized'):
        await service.preview(identity, spec)
    assert service._source(identity)[0] == raw_before
    single = await service.preview(identity, {k: v for k, v in spec.items() if k != 'key_separator'})
    assert single['record_count'] == 1


async def test_heading_selector_is_filtered_by_start_pattern(config):
    service = DocumentTools(config)
    identity = source(service, '<p>Overview</p><p>Item 1</p><p>Continuation</p><p>Item 2</p><p>More</p>')
    preview = await service.preview(identity, {'mode': 'sections', 'selector': 'p',
        'heading_selector': 'p', 'start_pattern': '^Item', 'key_pattern': '^Item ([0-9]+)'})
    assert preview['record_count'] == 2
    assert preview['samples'][0]['paragraph_count'] == 2


async def test_agent_candidate_review_rejects_then_accepts_revision(config, monkeypatch):
    from cdp_browser_agent.documents import review
    calls = AsyncMock(side_effect=[json.dumps({'accepted': False, 'issues': [{'kind': 'record_unit', 'evidence_id': 'record_0',
        'quote': 'Overview', 'problem': 'Overview is not a product.'}],
        'record_unit': 'product', 'sample_checks': [{'evidence_id': f'record_{i}', 'unit_matches': i != 0,
            'body_matches': True, 'reason': 'Overview is a structural heading' if i == 0 else 'Product row'} for i in range(3)],
        'coverage_checks': [],
        'required_changes': ['Select product rows only.']}), json.dumps({'accepted': True, 'issues': [], 'required_changes': [],
        'record_unit': 'product', 'sample_checks': [{'evidence_id': f'record_{i}', 'unit_matches': True,
            'body_matches': True, 'reason': 'Product row'} for i in range(2)], 'coverage_checks': [
            {'evidence_ids': ['unselected_peer_0'], 'matches_requested_record_unit': False,
             'disposition': 'retained_structure', 'reason': 'Overview retained outside product records'}]})])
    monkeypatch.setattr(review, 'chat_completion', calls)
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state['task'] = 'Export one object per product.'
        identity = source(runtime.documents, '<p>Overview</p><p class="product">A</p><p class="product">B</p>')
        bad = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': {'mode': 'elements', 'selector': 'p'}})
        assert bad['ok'], bad
        assert bad['status'] == 'needs_revision' and not bad['review']['accepted']
        assert any('exclude_pattern' in hint and 'remainder' in hint for hint in bad['review']['repair_options'])
        viewed = await runtime.registry.call('document_review', {'job_id': bad['job_id']})
        assert viewed['status'] == 'needs_revision'
        assert viewed['review']['accepted'] is False
        assert runtime.context()['document_candidates'][0]['review']['accepted'] is False
        blocked = await runtime.registry.call('document_export', {'job_id': bad['job_id']})
        assert not blocked['ok']
        repeated = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': {'mode': 'elements', 'selector': 'p'}})
        assert calls.await_count == 1  # Cached rejection cannot be bypassed by retry.
        good = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': {'mode': 'elements', 'selector': 'p.product'}})
        assert good['review']['accepted'] and not good['review']['semantic_accuracy_verified']
        assert (await runtime.registry.call('document_export', {'job_id': good['job_id']}))['ok']
        assert calls.await_count == 2


async def test_resumed_review_policy_change_invalidates_approval_and_recovers_saved_recipe(config, monkeypatch):
    from cdp_browser_agent.documents import review
    response = json.dumps({'accepted': True, 'record_unit': 'product', 'issues': [], 'required_changes': [],
        'sample_checks': [{'evidence_id': 'record_0', 'unit_matches': True, 'body_matches': True, 'reason': 'Complete product'}],
        'coverage_checks': []})
    calls = AsyncMock(return_value=response)
    monkeypatch.setattr(review, 'chat_completion', calls)
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state['task'] = 'Export one object per product.'
        identity = source(runtime.documents, '<article>Product A</article>')
        spec = {'mode': 'elements', 'selector': 'article'}
        first = await runtime.registry.call('document_preview', {'source_id': identity, 'spec': spec})
        assert first['review']['accepted']
        monkeypatch.setattr(review, 'SYSTEM', review.SYSTEM + '\nUpdated review requirements.\n')
        assert not (await runtime.registry.call('document_export', {'job_id': first['job_id']}))['ok']
        context = runtime.context()
        refresh = context['document_review_refresh']
        assert len(refresh['actions']) == 1
        action = refresh['actions'][0]
        assert action['arguments'] == {'source_id': identity, 'spec': spec}
        assert context['document_candidates'][0]['status'] == 'review_required'
        updated = await runtime.registry.call(action['name'], action['arguments'])
        assert updated['review']['accepted'] and calls.await_count == 2
        assert updated['review']['policy_id'] != first['review']['policy_id']
        assert not runtime.context()['document_review_refresh']['actions']
        assert (await runtime.registry.call('document_export', {'job_id': first['job_id']}))['ok']


async def test_review_projection_distinguishes_display_limits_and_retained_material(config):
    from cdp_browser_agent.documents.review import build_evidence
    service = DocumentTools(config)
    identity = source(service, '<h2 class="item">Item A</h2><p class="body">' + 'Long body. ' * 400 + '</p>'
        '<p class="note">Separate revision history</p><p class="body-indent">Important continuation</p>'
        '<h2 class="item">[Item B retired]</h2><table><tr><td>Prices</td></tr></table>')
    preview = await service.preview(identity, {'mode': 'sections', 'selector': 'h2.item, p.body, p.note',
        'heading_selector': 'h2.item', 'start_pattern': '^Item', 'body_selector': 'p.body'})
    _, receipt, candidate = service._job(preview['job_id'])
    projection = build_evidence(service, receipt, candidate)
    assert 'annotations' in projection['output_shape']['record_fields']
    assert projection['output_shape']['remainder_retained_in_output']
    assert projection['samples'][0]['annotation_samples'][0]['text'] == 'Separate revision history'
    body = next(e for e in projection['evidence'] if e['id'] == 'record_0')
    assert body['text_preview_truncated'] and body['text_chars'] > 4000
    assert any(e['text'] == 'Important continuation' and e['location'] == 'remainder' for e in projection['evidence'])
    assert any(e['text'] == '[Item B retired]' and e['id'].startswith('filtered_heading') for e in projection['evidence'])
    assert candidate['coverage']['remainder_tags']['table'] == 1
    assert preview['samples'][0]['text_preview_truncated']


async def test_review_scopes_are_computed_from_all_selected_xml_records(config):
    from cdp_browser_agent.documents.review import build_evidence
    service = DocumentTools(config)
    identity = source(service, '<Catalog><Main><Part><Name>A</Name></Part></Main>'
        '<Supplement><Part><Name>B</Name></Part></Supplement><Table>Prices</Table></Catalog>', 'xml')
    preview = await service.preview(identity, {'mode': 'elements', 'selector': './/Part', 'title_selector': 'Name'})
    _, receipt, candidate = service._job(preview['job_id'])
    projection = build_evidence(service, receipt, candidate)
    assert sum(projection['record_scopes'].values()) == 2
    assert any('Supplement' in scope for scope in projection['record_scopes'])
    assert projection['unselected_peer_group_count'] == 0


async def test_reviewer_unsupported_quotes_retry_bounded_and_fail_inconclusive(config, monkeypatch):
    from cdp_browser_agent.documents import review
    service = DocumentTools(config)
    identity = source(service, '<article>Actual record</article>')
    preview = await service.preview(identity, {'mode': 'elements', 'selector': 'article'})
    invented = json.dumps({'accepted': False, 'issues': [{'kind': 'body_omission', 'evidence_id': 'record_0',
        'quote': 'Invented missing product', 'problem': 'Missing row'}], 'required_changes': ['Invent another row'],
        'record_unit': 'article', 'sample_checks': [{'evidence_id': 'record_0', 'unit_matches': True,
            'body_matches': True, 'reason': 'Article'}], 'coverage_checks': []})
    calls = AsyncMock(return_value=invented)
    monkeypatch.setattr(review, 'chat_completion', calls)
    decision = await review.review_candidate(config, 'One object per article', service, preview['job_id'])
    assert calls.await_count == 2
    assert decision['status'] == 'review_inconclusive' and not decision['accepted']
    assert decision['issues'] == [] and not decision['evidence_quotes_verified']
    evidence_path = service._path(preview['job_id'], 'jobs') / 'reviews' / (decision['review_id'][:32] + '.evidence.json')
    assert len(json.loads(evidence_path.read_text(encoding='utf-8'))['attempts']) == 2


def test_reviewer_cannot_accept_without_checking_units_or_with_failed_samples():
    from cdp_browser_agent.documents.review import validate_decision
    evidence = [{'id': 'record_0', 'text': 'Catalog heading', 'location': 'record_body'}]
    decision = {'accepted': True, 'record_unit': 'product', 'issues': [], 'required_changes': [], 'sample_checks': [], 'coverage_checks': []}
    with pytest.raises(ValueError, match='each record_body'):
        validate_decision(json.dumps(decision), evidence)
    decision['sample_checks'] = [{'evidence_id': 'record_0', 'unit_matches': False,
        'body_matches': True, 'reason': 'A catalog heading is not a product'}]
    with pytest.raises(ValueError, match='failed sample'):
        validate_decision(json.dumps(decision), evidence)
    decision.update(accepted=False, issues=[{'kind': 'body_omission', 'evidence_id': 'record_0',
        'quote': 'Catalog heading', 'problem': 'This text implies missing body paragraphs'}])
    with pytest.raises(ValueError, match='outside the body'):
        validate_decision(json.dumps(decision), evidence)


def test_valid_rejection_survives_unfinished_checklist_but_acceptance_does_not():
    from cdp_browser_agent.documents.review import validate_decision
    evidence = [{'id': 'record_0', 'text': 'Mounting procedure', 'location': 'record_body'},
                {'id': 'unselected_peer_0', 'text': 'Tighten the mounting screws.', 'location': 'remainder'},
                {'id': 'unselected_peer_1', 'text': 'Revision history', 'location': 'remainder'}]
    decision = {'accepted': False, 'record_unit': 'procedure',
        'sample_checks': [],
        'coverage_checks': [{'evidence_ids': ['unselected_peer_1'], 'matches_requested_record_unit': False,
            'disposition': 'retained_annotations', 'reason': 'Historical material retained'}],
        'issues': [{'kind': 'body_omission', 'evidence_id': 'unselected_peer_0',
            'quote': 'Tighten the mounting screws.', 'problem': 'A required mounting step is outside the procedure body.'}],
        'required_changes': ['Include the omitted mounting step in its procedure.']}
    rejected = validate_decision(json.dumps(decision), evidence)
    assert not rejected['accepted'] and not rejected['checklist_verified']
    assert rejected['review_scope'] == 'quoted_issues_only' and rejected['issues'] == decision['issues']
    assert 'sample_checks' not in rejected and 'coverage_checks' not in rejected
    decision.update(accepted=True, issues=[], required_changes=[])
    with pytest.raises(ValueError, match='each record_body'):
        validate_decision(json.dumps(decision), evidence)
    decision['sample_checks'] = [{'evidence_id': 'record_0', 'unit_matches': True,
        'body_matches': True, 'reason': 'Procedure body'}]
    with pytest.raises(ValueError, match='unselected_peer_1.*remainder'):
        validate_decision(json.dumps(decision), evidence)


async def test_review_samples_late_structural_variants_and_requires_negative_space_checks(config):
    from cdp_browser_agent.documents.review import build_evidence, validate_decision
    service = DocumentTools(config)
    markup = ''.join(f'<h2 class="item">Item {i}</h2><p class="body">Body {i}</p>' +
        ('<p style="text-align:center"><strong>Next group</strong></p>' if i == 23 else '') for i in range(75))
    identity = source(service, markup)
    preview = await service.preview(identity, {'mode': 'sections', 'selector': 'h2.item,p', 'heading_selector': 'h2.item'})
    _, receipt, candidate = service._job(preview['job_id'])
    projection = build_evidence(service, receipt, candidate)
    assert {0, 23, 37, 74} <= {s['index'] for s in projection['samples']}
    assert len(projection['samples']) <= 7
    assert 'Next group' in next(e['text'] for e in projection['evidence'] if e['id'] == 'record_23')
    evidence = [{'id': 'record_0', 'text': 'Item A', 'location': 'record_body'},
                {'id': 'filtered_heading_0', 'text': 'Item B retired', 'location': 'annotation'}]
    decision = {'accepted': True, 'record_unit': 'item', 'issues': [], 'required_changes': [],
        'sample_checks': [{'evidence_id': 'record_0', 'unit_matches': True, 'body_matches': True, 'reason': 'Item A'}],
        'coverage_checks': []}
    with pytest.raises(ValueError, match='every coverage_evidence_id'):
        validate_decision(json.dumps(decision), evidence)
    decision['coverage_checks'] = [{'evidence_ids': ['filtered_heading_0'], 'matches_requested_record_unit': True,
        'disposition': 'missing_record', 'reason': 'B needs a separate row'}]
    with pytest.raises(ValueError, match='Missing requested records'):
        validate_decision(json.dumps(decision), evidence)
    decision['coverage_checks'][0]['disposition'] = 'retained_annotations'
    with pytest.raises(ValueError, match='storage in annotations'):
        validate_decision(json.dumps(decision), evidence)


async def test_fetch_provenance_runtime_delivery_and_completion_gate(config):
    async with ExtensionRuntime(config) as runtime:
        service = runtime.documents
        raw = b'<html><article><h2>Product</h2><p>Specification</p></article></html>'
        original = service.root / 'input.bin'
        original.parent.mkdir(parents=True)
        original.write_bytes(raw)
        runtime.web.fetch = AsyncMock(return_value={'ok': True, 'response_sha256': digest(raw),
            'artifact_paths': ['', str(original)], 'url': 'https://example.org', 'content_type': 'text/html'})
        opened = await runtime.registry.call('document_open', {'url': 'https://example.org'})
        assert opened['ok']
        preview = await runtime.registry.call('document_preview', {'source_id': opened['source_id'],
            'spec': {'mode': 'elements', 'selector': 'article'}})
        config['agent']['completion_documents'] = {'min_sources': 1}
        assert not check_documents(config, runtime.task_state)['ok']
        exported = await runtime.registry.call('document_export', {'job_id': preview['job_id']})
        assert check_documents(config, runtime.task_state)['ok']
        assert exported['output_path'] in runtime.task_state['collected_files']
        assert runtime.context()['external_tool_count'] == 0
        (service._path(preview['job_id'], 'jobs') / 'export.json').write_text('{}')
        assert not check_documents(config, runtime.task_state)['ok']


async def test_mcp_generic_inspect_preview_export(config):
    service = DocumentTools(config)
    identity = source(service, '<article><h1>News</h1><p>Source content</p></article>')
    async with Client(create_mcp_server(config)) as client:
        inspected = await client.call_tool('document_inspect', {'source_id': identity, 'selector': 'article'})
        assert not inspected.is_error
        preview = await client.call_tool('document_preview', {'source_id': identity, 'spec': {'mode': 'elements', 'selector': 'article'}})
        assert not preview.is_error
        result = await client.call_tool('document_export', {'job_id': preview.structured_content['job_id']})
        assert result.structured_content['replay_matches']
        bad = await client.call_tool('document_preview', {'source_id': identity, 'spec': {'mode': 'python', 'selector': 'article'}})
        assert bad.is_error


async def test_full_document_export_handoff_to_worker_by_identity(config, tmp_path, monkeypatch):
    from cdp_browser_agent.processing import engine
    monkeypatch.setattr(engine, 'chat_completion', AsyncMock(side_effect=AssertionError('Exact mapping needs no model')))
    method = tmp_path / 'format.json'
    method.write_text(json.dumps({'name': 'format', 'mode': 'mapping', 'instructions': 'Copy source body exactly.',
        'field_map': {'body': 'text'}, 'formats': ['json'], 'output_schema': {'type': 'object',
        'properties': {'body': {'type': 'string'}}, 'required': ['body'], 'additionalProperties': False}}), encoding='utf-8')
    config['processing'].update(paths=[str(method)], artifact_dir=str(tmp_path / 'workers'))
    service = DocumentTools(config)
    identity = source(service, '<entry><p>Alpha</p></entry><entry><p>Beta</p></entry>')
    preview = await service.preview(identity, {'mode': 'elements', 'selector': 'entry'})
    await service.export(preview['job_id'])
    async with Client(create_mcp_server(config)) as client:
        result = await client.call_tool('browser_worker_start', {'profile': 'format', 'document_job_id': preview['job_id']})
        assert not result.is_error
        assert result.structured_content['ok']
        assert result.structured_content['model_calls'] == 0
        assert result.structured_content['validated_count'] == 2
        invalid = await client.call_tool('browser_worker_start', {'profile': 'format', 'document_job_id': preview['job_id'], 'records': []})
        assert invalid.is_error
