"""Frozen live processing of non-legal sources not used by implementation tests.

Only URLs and semantic requirements enter the task. Independent source-unit
assertions run after the agent stops; no selector or answer is fed back.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.documents.engine import DocumentTools, digest
from cdp_browser_agent.harness.verification import check_documents
from cdp_browser_agent.workflows.store import atomic_json
from scripts.live_document_eval_support import snapshot, trace_model, model_costs


URLS = {'pep_index': 'https://peps.python.org/', 'nasa_feed': 'https://www.nasa.gov/rss/dyn/breaking_news.rss'}


def identify(service, law):
    meta = law['source']
    while meta.get('parent_source_id'):
        _, meta = service._source(meta['parent_source_id'])
    return next((name for name, url in URLS.items() if meta.get('requested_url', meta.get('url')) == url), None)


def oracle(name, candidate, service):
    """Compare delivered units to actual downloaded source, after execution only."""
    raw, meta = service._source(candidate['source_id'])
    tree = service._tree(raw, meta)
    if name == 'pep_index':
        expected = [n for n in tree.select('tr') if any(re.search(r'(?:^|/)pep-[0-9]+/?(?:[?#].*)?$', str(a.attrs.get('href', '')))
            for a in tree.select('a', n))]
    else:
        expected = [n for n in tree.nodes.values() if n.tag.split('}')[-1] == 'item']
    rows = candidate['records']
    normalize = lambda text: re.sub(r'\s+', '', text)
    matches = lambda node: [row for row in rows if node.path in row['source_paths']]
    return {'source_has_expected_units': bool(expected), 'record_count_matches_source_units': len(rows) == len(expected),
        'each_source_unit_has_one_record': all(len(matches(node)) == 1 for node in expected),
        'full_unit_text_in_own_record': all(any(normalize(node.text()) in normalize(row['text']) for row in matches(node)) for node in expected),
        'source_tree_conserved': candidate['coverage']['tree_conserved'],
        'row_text_hashes': all(digest(row['text'].encode()) == row['text_sha256'] for row in rows)}, len(expected)


async def evaluate(tag, config_path=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', tag):
        raise ValueError('Use a unique simple tag')
    log = ROOT / 'logs/live-document-transfer' / tag
    output = ROOT / 'deliveries/document-transfer' / tag
    log.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    config_path = Path(config_path or ROOT / 'examples/documents-gpt-6-luna.json').resolve()
    config = load_config(str(config_path))
    config['agent'].update(max_steps=40, log_dir=str(log / 'agent'), completion_documents={'min_sources': 2, 'collection_key': 'records'})
    config['harness'].update(run_timeout_seconds=600, state_dir=str(log / 'state'), artifact_dir=str(log / 'artifacts'))
    config['web']['artifact_dir'] = str(log / 'web')
    config['documents']['state_dir'] = str(output / 'workspace')
    atomic_json(log / 'config.json', config)
    task = ('下载并分别整理以下两个非法律文档，输出各自一个 JSON 对象，records 数组每个条目一个对象。'
        '第一个是 Python PEP 索引，每个索引条目单独成对象；同一个 PEP 在不同分组出现时保留各次来源位置。'
        '第二个是 NASA 新闻订阅源，每个新闻条目单独成对象。保留条目的完整原文、来源、属性和其他材料，'
        '不访问每条链接去扩展内容，也不要把导航或索引说明变成条目。根据实际文档自行选择处理方法。'
        '检验输出后报告真实文件路径、条数和任何无法完成的部分。\n' + json.dumps(URLS, ensure_ascii=False))
    atomic_json(log / 'task.json', {'task': task, 'recipes_supplied': False, 'postrun_oracle_only': True})
    hashes = snapshot(ROOT, log, [Path(__file__), ROOT / 'scripts/live_document_eval_support.py', config_path])
    with trace_model(log, config) as wire:
        state = await run_browser_agent(task, config)
    atomic_json(log / 'agent-state.json', state)
    service = DocumentTools(config)
    checks = {'agent_completed': state['status'] == 'completed', 'artifact_gate_passed': check_documents(config, state)['ok'],
        'source_code_unchanged': all(digest((ROOT / p).read_bytes()) == value for p, value in hashes.items()),
        'no_special_mcp_or_skill': not config['harness']['mcp_servers'] and not config['harness']['active_skills']}
    counts, expected_counts = {}, {}
    for job_id in state.get('document_exports', {}):
        folder, _, candidate = service._job(job_id)
        name = identify(service, candidate)
        if name:
            result, expected = oracle(name, candidate, service)
            checks.update({f'{name}_{k}': v for k, v in result.items()})
            counts[name], expected_counts[name] = len(candidate['records']), expected
            shutil.copyfile(folder / 'export.json', output / f'{name}.json')
    checks.update({f'{name}_exported': name in counts for name in URLS})
    receipt = {'all_passed': all(checks.values()), 'checks': checks, 'agent_status': state['status'],
        'counts': counts, 'expected_counts': expected_counts, 'model_requests': len(wire), 'model_costs': model_costs(wire),
        'metrics': state.get('metrics'), 'source_sha256': hashes,
        'scope': 'Two new public documents, known-source processing only. No claim of general document reliability or source discovery.'}
    atomic_json(log / 'receipt.json', receipt)
    atomic_json(output / 'validation.json', receipt)
    print(json.dumps({k: receipt[k] for k in ('all_passed', 'checks', 'counts', 'model_requests', 'model_costs')}, ensure_ascii=False), flush=True)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--config', help='Model/config JSON; defaults to documents-gpt-6-luna.json. Credentials come from its apiKeyEnv.')
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.tag, config_path=args.config))
    raise SystemExit(0 if result['all_passed'] else 1)
