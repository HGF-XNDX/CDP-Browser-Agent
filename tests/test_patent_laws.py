import base64
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from examples.patent_laws.collector import (PatentCollection, create_server, parse_cn,
    parse_us, parse_jp, sha)


def cn_source():
    def number(n):
        digits = "零一二三四五六七八九"
        return digits[n] if n < 10 else (digits[n//10] if n >= 20 else "") + "十" + (digits[n%10] if n%10 else "")
    return ('<html><div class="article-content"><p>第一章 总则</p>' + ''.join(
        f'<p>第{number(i)}条 原文内容{i}。</p><p>同条第二段{i}。</p>' for i in range(1, 83)) + '</div></html>').encode()


def us_source():
    head = '<!-- AUTHORITIES-PUBLICATION-NAME:2024 Main Edition --><!-- AUTHORITIES-LAWS-ENACTED-THROUGH-DATE:20250106 --><h1>TITLE 35</h1>'
    return (head + ''.join(f'<!-- documentid:35_{i} currentthrough:20250106 -->'
        f'<h3 class="section-head">§{i}. Heading</h3><!-- field-start:statute -->'
        f'<p class="statutory-body">Source {i} with in<!-- comment -->line text.</p><!-- field-end:statute -->'
        f'<!-- field-start:notes --><p class="note-body">Historical quote §{i+1}.</p>' for i in range(1, 101)) +
        '<!-- documentid:35_155 -->'
        '<h3 class="section-head">[§§155, 155A. Repealed. Public Law]</h3>').encode()


def jp_source():
    article = lambda n: f'<Article Num="{n}"><ArticleTitle>第{n}条</ArticleTitle><Paragraph Num="1"><ParagraphNum/><ParagraphSentence><Sentence>原文{n}。</Sentence></ParagraphSentence></Paragraph></Article>'
    xml = '<Law><LawBody><LawTitle>特許法</LawTitle><MainProvision>' + ''.join(article(str(n)) for n in range(1, 191)) + article('1_2') + '</MainProvision><SupplProvision AmendLawNum="修正法"><SupplProvisionLabel>附則</SupplProvisionLabel>' + article('1') + '<Paragraph Num="2"><ParagraphSentence><Sentence>附則の番号のない条。</Sentence></ParagraphSentence></Paragraph></SupplProvision></LawBody></Law>'
    return json.dumps({'law_info': {'law_id': '334AC0000000121'},
        'revision_info': {'amendment_enforcement_date': '2026-06-24'},
        'law_full_text': base64.b64encode(xml.encode()).decode()}).encode()


def test_cn_all_articles_keep_continuation_paragraphs():
    result = parse_cn(cn_source())
    assert len(result['articles']) == 82
    assert result['articles'][1]['paragraphs'] == ['原文内容2。', '同条第二段2。']
    with pytest.raises(ValueError, match='1..82'):
        parse_cn(cn_source().replace('第八十二条'.encode(), '第八十一条'.encode()))


def test_us_notes_do_not_become_statutes_and_grouped_numbers_split():
    result = parse_us(us_source())
    assert len(result['articles']) == 102
    assert result['articles'][0]['paragraphs'] == ['Source 1 with inline text.']
    assert all('Historical quote' not in a['text'] for a in result['articles'])
    assert [a['number'] for a in result['articles'][-2:]] == ['155', '155A']
    assert all(a['status'] == 'repealed' for a in result['articles'][-2:])
    assert result['version']['laws_enacted_through'] == '2025-01-06'
    assert not result['version']['latest_available_law_verified']
    with pytest.raises(ValueError, match='Unmapped text'):
        parse_us(us_source().replace(b'<!-- field-end:statute -->', b'unmapped<!-- field-end:statute -->', 1))


def test_jp_article_suffix_scope_and_non_article_supplements_preserved():
    result = parse_jp(jp_source(), '2026-09-30')
    assert len(result['articles']) == 192
    ids = [a['article_id'] for a in result['articles']]
    assert len(set(ids)) == len(ids) and 'JP:main:1_2' in ids and 'JP:supplementary:1:1' in ids
    assert result['supplementary_provisions'][0]['unnumbered_article_paragraphs'] == ['附則の番号のない条。']
    with pytest.raises(ValueError, match='future'):
        parse_jp(jp_source(), '2026-01-01')
    value = json.loads(jp_source())
    value['law_full_text'] = base64.b64encode(b'<!DOCTYPE Law [<!ENTITY injected "data">]><Law/>').decode()
    with pytest.raises(ValueError, match='Unexpected'):
        parse_jp(json.dumps(value).encode(), '2026-09-30')


def test_jp_attached_table_is_not_lost_when_article_counts_pass():
    value = json.loads(jp_source())
    xml = base64.b64decode(value['law_full_text']).decode()
    xml = xml.replace('</LawBody>', '<AppdxTable Num="1"><AppdxTableTitle>別表</AppdxTableTitle><TableStruct><Table><TableRow><TableColumn>手数料</TableColumn><TableColumn>金額</TableColumn></TableRow></Table></TableStruct></AppdxTable></LawBody>')
    value['law_full_text'] = base64.b64encode(xml.encode()).decode()
    result = parse_jp(json.dumps(value).encode(), '2026-09-30')
    assert result['validation']['source_appendix_table_count'] == result['validation']['mapped_appendix_table_count'] == 1
    table = result['appendices'][0]
    assert table['text'] == '別表手数料金額'
    row = table['structure']['children'][1]['children'][0]['children'][0]
    assert [c['text'] for c in row['children']] == ['手数料', '金額']


async def test_real_mcp_fetch_worker_export_resume_and_hash_rejection(tmp_path, monkeypatch):
    config = browser_agent_default_config()
    config['agent']['log_dir'] = str(tmp_path/'logs')
    collection = PatentCollection(config, tmp_path/'delivery', '2026-09-30')
    response = cn_source()
    download = AsyncMock(return_value={'url': 'https://www.cnipa.gov.cn/source', 'status_code': 200,
        'headers': {'content-type': 'text/html; charset=utf-8'}, 'body': response, 'redirects': []})
    monkeypatch.setattr('cdp_browser_agent.web.tools.download', download)
    # The configured method must work without any model service.
    monkeypatch.setattr('cdp_browser_agent.processing.engine.chat_completion', AsyncMock(side_effect=AssertionError('No rewriting')))
    async with Client(create_server(collection)) as client:
        assert len((await client.list_tools()).tools) == 3
        assert not (await client.call_tool('patent_source_fetch', {'country': 'CN'})).is_error
        exported = await client.call_tool('patent_articles_export', {'country': 'CN'})
        assert not exported.is_error, exported
        result = exported.structured_content
        assert result['ok'] and result['article_count'] == 82 and result['processing_model_calls'] == 0
        path = Path(result['output_path'])
        original = path.read_bytes()
        repeat = await client.call_tool('patent_articles_export', {'country': 'CN'})
        assert not repeat.is_error and path.read_bytes() == original
        assert repeat.structured_content['worker_session_id'] == result['worker_session_id']
        assert (await client.call_tool('patent_source_fetch', {'country': 'CN'})).structured_content['reused']
        assert download.await_count == 1
        raw = next((tmp_path/'delivery'/'CN'/'source').rglob('response.bin'))
        raw.write_bytes(b'changed')
        assert (await client.call_tool('patent_articles_export', {'country': 'CN'})).is_error
        assert path.read_bytes() == original


def test_unknown_country_cannot_select_a_file(tmp_path):
    collection = PatentCollection(browser_agent_default_config(), tmp_path, '2026-09-30')
    with pytest.raises(ValueError, match='country'):
        collection.folder('../outside')
