"""Opt-in acceptance of unseeded planner recipes through generic document tools.

Country-specific selectors occur ONLY in independent post-run assertions; never
in the planner's task, tool descriptions, config, skills or runtime. Known official
URLs are task inputs: this evaluates processing, not source discovery.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.documents.engine import DocumentTools, digest
from cdp_browser_agent.harness.verification import check_documents
from cdp_browser_agent.harness.task_store import TaskStore
from cdp_browser_agent.workflows.store import atomic_json
from scripts.live_document_eval_support import snapshot, trace_model, model_costs

URLS = {
    'CN': 'https://www.cnipa.gov.cn/art/2020/11/23/art_97_155167.html',
    'US': 'https://www.govinfo.gov/content/pkg/USCODE-2024-title35/html/USCODE-2024-title35.htm',
    'JP': 'https://laws.e-gov.go.jp/api/2/law_data/334AC0000000121?asof=2026-09-30&law_full_text_format=xml',
}


def compact(text):
    return re.sub(r'\s+', '', text)


def tree_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return ''.join(tree_text(n) for n in node.get('content', []))
    return ''


def oracle(country, law, service):
    """Gold assertions are isolated from generation. Never repair a candidate here."""
    rows = law.get('articles', [])
    raw, meta = service._source(law['source_id'])
    body = compact('\n'.join(r['text'] for r in rows))
    checks = {'has_articles': bool(rows), 'all_text_hashes': all(digest(r['text'].encode()) == r['text_sha256'] for r in rows),
        'unique_record_ids': len({r['id'] for r in rows}) == len(rows), 'source_tree_conserved': law['coverage']['tree_conserved']}
    if country == 'CN':
        soup = BeautifulSoup(raw, 'html.parser')
        paragraphs = [p.get_text().strip() for p in soup.select('.article-content p')]
        headers = [p for p in paragraphs if re.match(r'^第[零一二三四五六七八九十百千]+条\s', p)]
        checks['all_article_boundaries'] = len(rows) == len(headers) == 82
        row_bodies = [compact(r['text']) for r in rows]
        checks['each_heading_in_distinct_record'] = all(sum(compact(h) in r for r in row_bodies) == 1 for h in headers)
        active, content = False, []
        for p in paragraphs:
            if re.match(r'^第[零一二三四五六七八九十百千]+条\s', p):
                active = True
            if active and not re.match(r'^第[一二三四五六七八九十]+章', p):
                content.append(p)
        checks['all_continuation_paragraphs'] = all(compact(p) in body for p in content)
        chapters = {compact(p) for p in paragraphs if re.match(r'^第[一二三四五六七八九十]+章', p)}
        checks['chapter_titles_not_body_paragraphs'] = not any(compact(p) in chapters for r in rows for p in r['paragraphs'])
        expected, current = [], None
        for p in paragraphs:
            if re.match(r'^第[零一二三四五六七八九十百千]+条\s', p):
                current = [p]
                expected.append(current)
            elif current is not None and not re.match(r'^第[一二三四五六七八九十]+章', p):
                current.append(p)
        checks['body_bound_to_own_article'] = len(rows) == len(expected) and all(
            compact(r['text']) == compact(''.join(parts)) for r, parts in zip(rows, expected))
    elif country == 'US':
        soup = BeautifulSoup(raw, 'html.parser')
        headings = soup.select('h3.section-head')
        paragraphs = soup.select('p[class^="statutory-body"]')
        checks['all_section_headings'] = len(headings) == 175 and all(any(compact(h.get_text()) == compact(r['heading']) for r in rows) for h in headings)
        checks['grouped_inactive_sections_separate'] = len(rows) == 176
        checks['all_statutory_paragraphs'] = len(paragraphs) == 1161 and all(compact(p.get_text()) in body for p in paragraphs)
        checks['annotations_not_body'] = not any('Editorial Notes' in r['text'] for r in rows)
        checks['note_nodes_not_body'] = all(
            any(a['path'] == path for a in row['annotations'])
            for row in rows for path in row['source_paths']
            if any(c.startswith('note-body') for c in law['fragments'][path]['attrs'].get('class', [])))
        expected = []
        for heading in headings:
            body_parts = []
            for node in heading.find_all_next():
                if node.name == 'h3' and 'section-head' in node.get('class', []):
                    break
                if node.name == 'p' and any(c.startswith('statutory-body') for c in node.get('class', [])):
                    body_parts.append(node.get_text())
            expected.append((compact(heading.get_text()), body_parts))
        checks['body_bound_to_own_section'] = all(
            all(compact(p) in compact(r['text']) for p in parts)
            for h, parts in expected for r in rows if compact(r['heading']) == h)
    else:
        if meta['format'] != 'xml':
            return {**checks, 'decoded_xml': False}
        xml = ET.fromstring(raw)
        articles = xml.findall('.//Article')
        # Article nodes can hold a combined deleted-number range. Node fidelity
        # and one output per numbered article are different acceptance questions.
        tree = service._tree(raw, meta)
        article_nodes = tree.select('.//Article')
        checks['all_article_nodes'] = len(articles) == 464 and all(
            any(node.path in row['source_paths'] for row in rows) for node in article_nodes)
        checks['grouped_article_ranges_separate'] = all(
            sum(node.path in row['source_paths'] for row in rows) ==
            (int(parts[1]) - int(parts[0]) + 1 if len(parts := article.get('Num', '').split(':')) == 2
             and all(part.isdigit() for part in parts) else 1)
            for article, node in zip(articles, article_nodes))
        saved = [compact(tree_text(law['fragments'][p])) for r in rows for p in r['source_paths']]
        checks['each_article_source_preserved'] = all(compact(''.join(a.itertext())) in saved for a in articles)
        checks['all_body_paragraphs'] = all(compact(''.join(p.itertext())) in body for a in articles for p in a.findall('Paragraph'))
        checks['body_bound_to_own_article'] = all(
            all(compact(''.join(p.itertext())) in compact(r['text']) for p in a.findall('Paragraph'))
            for a, node in zip(articles, article_nodes) for r in rows if node.path in r['source_paths'])
        checks['captions_in_records'] = all(
            a.find('ArticleCaption') is None or compact(''.join(a.find('ArticleCaption').itertext())) in
            compact(r['heading'] + r['text'] + ''.join(n['text'] for n in r['annotations']))
            for a, node in zip(articles, article_nodes) for r in rows if node.path in r['source_paths'])
        checks['appendix_table_retained'] = len(xml.findall('./LawBody/AppdxTable')) == 1 and law['coverage']['remainder_tags'].get('AppdxTable') == 1
        checks['source_revision_retained'] = bool(meta.get('parent_source_id'))
    return checks


async def evaluate(tag, resume=False, config_path=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', tag):
        raise ValueError('Use a simple unique run tag')
    log = ROOT / 'logs/live-generic-documents' / tag
    output = ROOT / 'deliveries/generic-documents' / tag
    resume_run_id = None
    config_path = Path(config_path or ROOT / 'examples/documents-gpt-6-luna.json').resolve()
    if resume:
        config = json.loads((log / 'config.json').read_text(encoding='utf-8'))
        with_task = TaskStore(config)
        try:
            tasks = with_task.list()
            if len(tasks) != 1 or tasks[0]['status'] == 'completed':
                raise ValueError('Resume needs exactly one unfinished task in this run')
            previous = with_task.get(tasks[0]['run_id'])
        finally:
            with_task.close()
        resume_run_id = previous['run_id']
        attempt = previous.get('attempt', 1) + 1
        log, output = log / f'attempt-{attempt}', output / f'attempt-{attempt}'
    else:
        config = load_config(str(config_path))
        config['agent'].update(log_dir=str(log / 'agent'), completion_documents={'min_sources': 3, 'collection_key': 'articles'})
        config['harness'].update(state_dir=str(log / 'state'), artifact_dir=str(log / 'artifacts'))
        config['web']['artifact_dir'] = str(log / 'web')
        config['documents']['state_dir'] = str(output / 'workspace')
    log.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(log / 'config.json', config)
    if resume:
        atomic_json(log / 'prior-checkpoint.json', previous)
    hashes = snapshot(ROOT, log, [Path(__file__), ROOT / 'scripts/live_document_eval_support.py', config_path])
    task = ('从以下官方网站下载中国、美国、日本各一部专利法，分别输出每部法律一个JSON对象，articles数组每条一个元素。'
        '请根据实际文档自行研究处理方法。保留完整原文、款项、来源及可核验版本，区分正文和注释。'
        '废止或改号的条目也必须在articles数组中各有一个独立对象，不能只放进相邻条目的注释。'
        '同一标题列有多个条号的，应按条号分别生成对象并保留共同来源。章节标题不能混入条文正文；附则和附表不能丢失。'
        '检验完整性后给出真实文件路径、条数和版本局限。网址只是起点，未提供网站专用方法。\n' + json.dumps(URLS, ensure_ascii=False))
    if resume:
        task = previous['task']
    atomic_json(log / 'task.json', {'task': task, 'recipes_supplied': False, 'known_urls_supplied': True})
    with trace_model(log, config) as wire:
        state = await run_browser_agent(task, config, resume_run_id=resume_run_id)
    atomic_json(log / 'agent-state.json', state)
    service = DocumentTools(config)
    checks = {'agent_completed': state['status'] == 'completed', 'artifact_gate_passed': check_documents(config, state)['ok'],
        'no_special_mcp_or_skill': not config['harness']['mcp_servers'] and not config['harness']['active_skills'],
        'source_code_unchanged': all(digest((ROOT / p).read_bytes()) == value for p, value in hashes.items())}
    laws = {}
    for job_id in state.get('document_exports', {}):
        folder, receipt, law = service._job(job_id)
        country = next((c for c, u in URLS.items() if law['source'].get('url') == u), None)
        if country:
            laws[country] = law
            shutil.copyfile(folder / 'export.json', output / f'{country}.json')
    for country in URLS:
        checks[f'{country}_exported'] = country in laws
        if country in laws:
            checks.update({f'{country}_{key}': value for key, value in oracle(country, laws[country], service).items()})
    atomic_json(output / 'laws.json', {'laws': laws})
    receipt = {'all_passed': all(checks.values()), 'checks': checks, 'agent_status': state['status'],
        'resume_run_id': resume_run_id, 'attempt': state.get('attempt', 1),
        'model_calls': len(wire), 'metrics': state.get('metrics'), 'browser_started': state.get('browser_started'),
        'model_costs': model_costs(wire),
        'counts': {c: len(law.get('articles', [])) for c, law in laws.items()}, 'source_sha256': hashes,
        'recipes': {c: law['method'] for c, law in laws.items()}, 'scope': 'Known-source processing acceptance, not source discovery or a newest-law guarantee.'}
    atomic_json(log / 'receipt.json', receipt)
    atomic_json(output / 'validation.json', receipt)
    print(json.dumps({k: receipt[k] for k in ('all_passed', 'checks', 'counts', 'model_calls', 'browser_started')}, ensure_ascii=False), flush=True)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--config', help='Model/config JSON for new runs; defaults to documents-gpt-6-luna.json. Resume uses the checkpoint config.')
    parser.add_argument('--resume', action='store_true', help='Continue a checkpoint; keep earlier outputs and write a separate attempt receipt')
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.tag, resume=args.resume, config_path=args.config))
    raise SystemExit(0 if result['all_passed'] else 1)
