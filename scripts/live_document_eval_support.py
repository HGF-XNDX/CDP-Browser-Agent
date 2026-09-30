"""Opt-in live evaluation evidence; never supplies extraction methods."""
from contextlib import contextmanager
import json
import time
import zipfile

import httpx

from cdp_browser_agent.documents.engine import digest


def snapshot(root, log, extra_paths):
    paths = sorted(set(root.joinpath('cdp_browser_agent').rglob('*.py')) | set(extra_paths))
    hashes = {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in paths}
    with zipfile.ZipFile(log / 'source-snapshot.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(root))
    return hashes


@contextmanager
def trace_model(log, config):
    wire, original = [], httpx.AsyncClient.send
    async def traced(client, request, **kwargs):
        selected = str(request.url).startswith(config['model']['baseUrl']) and request.method == 'POST'
        if not selected:
            return await original(client, request, **kwargs)
        started = time.monotonic()
        payload = json.loads(request.content)
        role = 'review' if payload.get('messages', [{}])[0].get('content', '').startswith('Review ONE') else 'planner'
        record = {'request': payload, 'role': role}
        try:
            response = await original(client, request, **kwargs)
            await response.aread()
            record.update(status_code=response.status_code, response=response.json(), outcome='response_received')
            return response
        except BaseException as exc:
            record.update(outcome='request_failed', error_type=type(exc).__name__, error=str(exc)[:1500])
            raise
        finally:
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            wire.append(record)
            with (log / 'model-wire.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            response_data = record.get('response', {})
            print(json.dumps({'model_call': len(wire), 'role': role, 'seconds': record['elapsed_seconds'],
                'input_tokens': response_data.get('usage', {}).get('prompt_tokens'),
                'outcome': record['outcome'],
                'reply': response_data.get('choices', [{}])[0].get('message', {}).get('content', '')[:350]}, ensure_ascii=False), flush=True)
    httpx.AsyncClient.send = traced
    try:
        yield wire
    finally:
        httpx.AsyncClient.send = original


def model_costs(wire):
    result = {}
    for role in ('planner', 'review'):
        records = [r for r in wire if r['role'] == role]
        tokens = [r.get('response', {}).get('usage', {}).get('prompt_tokens') for r in records]
        known = [v for v in tokens if isinstance(v, int)]
        result[role] = {'requests': len(records), 'failed_requests': sum(r['outcome'] == 'request_failed' for r in records),
            'usage_known_requests': len(known), 'usage_missing_requests': len(records) - len(known),
            'input_tokens_total': sum(known) if known else None, 'input_tokens_max': max(known) if known else None,
            'input_tokens_mean': round(sum(known) / len(known), 1) if known else None,
            'elapsed_seconds_total': round(sum(r['elapsed_seconds'] for r in records), 3),
            'elapsed_seconds_mean': round(sum(r['elapsed_seconds'] for r in records) / len(records), 3) if records else None}
    return result
