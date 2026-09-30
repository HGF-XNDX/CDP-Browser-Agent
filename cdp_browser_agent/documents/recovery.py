"""Persistent progress accounting for source-bound document repair."""
from __future__ import annotations

from copy import deepcopy
from email.utils import parsedate_to_datetime
from pathlib import Path
import time
import math
from urllib.parse import urlsplit, urlunsplit

from .engine import digest


class DocumentRecovery:
    def __init__(self, state, service, settings):
        self.state, self.service = state, service
        options = settings.get('recovery', {})
        self.limits = {name: int(options.get(name, default)) for name, default in (
            ('max_revisions', 10), ('max_inspections', 16), ('unproductive_limit', 4), ('diagnostic_actions', 3), ('max_retrieval_attempts', 3))}
        if any(not 1 <= value <= 100 for value in self.limits.values()):
            raise ValueError('Document recovery limits must be between 1 and 100')

    def source_for(self, arguments):
        if arguments.get('source_id'):
            return arguments['source_id']
        if arguments.get('job_id'):
            candidate = self.state.get('document_candidates', {}).get(arguments['job_id'])
            if candidate:
                return candidate['source_id']
            return self.service._job(arguments['job_id'])[1]['source_id']
        return None

    def ledger(self, source_id):
        return self.state.setdefault('document_recovery', {}).setdefault(source_id, {
            'source_id': source_id, 'stage': 'inspect', 'revisions': 0, 'inspections': 0,
            'unproductive_actions': 0, 'diagnostic_actions_used': 0, 'effective_revisions': 0,
            'observations': {}, 'recipes': {}, 'candidates': [], 'issues': [], 'limits': self.limits})

    def _unproductive(self, ledger):
        ledger['unproductive_actions'] += 1
        if ledger['stage'] == 'diagnose':
            ledger['diagnostic_actions_used'] += 1
        elif ledger['unproductive_actions'] >= self.limits['unproductive_limit']:
            ledger['stage'] = 'diagnose'
        if ledger['diagnostic_actions_used'] >= self.limits['diagnostic_actions']:
            ledger.update(stage='blocked', limitation='Bounded diagnosis produced no effective candidate change.')

    def _blocked(self, ledger, message):
        self.state['document_guard_rejections'] = self.state.get('document_guard_rejections', 0) + 1
        return {'ok': False, 'status': 'recovery_action_required', 'errorType': 'document_recovery_guard',
            'source_id': ledger['source_id'], 'stage': ledger['stage'], 'message': message,
            'recovery': self.view(ledger), 'effects': 'Requested action was not executed.'}

    def before(self, name, arguments):
        if name == 'document_open':
            self.state['active_document_source_id'] = None
            self.state['pending_document_url'] = arguments['url']
            return self.before_access('document_open', arguments['url'])
        source_id = self.source_for(arguments)
        if not source_id:
            return None
        self.state['active_document_source_id'] = source_id
        ledger = self.ledger(source_id)
        if ledger['stage'] == 'blocked':
            return self._blocked(ledger, 'This source exhausted its repair budget. Process other pending sources or report the saved limitation; unchanged retries do not reopen it.')
        if name == 'document_preview':
            spec = {k: v for k, v in arguments['spec'].items() if k != 'metadata'}
            identity = digest(spec)
            if ledger['recipes'].get(identity, 0) >= 2:
                self._unproductive(ledger)
                return self._blocked(ledger, 'The same effective recipe was already evaluated. Change a source-bound selection/label/body operation, inspect new evidence, or handle another source. Changing metadata or reasons is not a repair.')
            if ledger['revisions'] >= self.limits['max_revisions']:
                ledger.update(stage='blocked', limitation='Candidate revision limit reached.')
                return self._blocked(ledger, ledger['limitation'])
            ledger['revisions'] += 1
            ledger['recipes'][identity] = ledger['recipes'].get(identity, 0) + 1
        if name == 'document_inspect':
            if ledger['inspections'] >= self.limits['max_inspections']:
                ledger.update(stage='blocked', limitation='Source inspection limit reached without delivery.')
                return self._blocked(ledger, ledger['limitation'])
            ledger['inspections'] += 1
        return None

    def candidate(self, result):
        _, receipt, candidate = self.service._job(result['job_id'])
        ledger = self.ledger(receipt['source_id'])
        rows = candidate[receipt['spec'].get('collection_key', 'records')]
        fingerprint = digest([{k: row[k] for k in ('key', 'heading', 'text', 'paragraphs', 'source_paths', 'annotations')} for row in rows])
        previous_id = ledger.get('latest_job_id')
        changed = ledger.get('output_fingerprint') != fingerprint
        summary = {'job_id': result['job_id'], 'output_fingerprint': fingerprint, 'record_count': len(rows),
            'effective_change': changed, 'changed_spec_fields': [], 'changed_record_fields': [], 'changed_source_paths': []}
        if previous_id:
            _, previous_receipt, previous = self.service._job(previous_id)
            summary['changed_spec_fields'] = sorted(k for k in set(receipt['spec']) | set(previous_receipt['spec'])
                if k != 'metadata' and receipt['spec'].get(k) != previous_receipt['spec'].get(k))
            old_rows = previous[previous_receipt['spec'].get('collection_key', 'records')]
            old = {(r['source_paths'][0], r['key']): r for r in old_rows}
            first_by_path = {}
            for row in old_rows:
                first_by_path.setdefault(row['source_paths'][0], row)
            fields, paths = set(), []
            for row in rows:
                before = old.get((row['source_paths'][0], row['key']), first_by_path.get(row['source_paths'][0], {}))
                different = {k for k in ('key', 'heading', 'text', 'paragraphs', 'source_paths', 'annotations') if row[k] != before.get(k)}
                fields.update(different)
                if different and len(paths) < 8:
                    paths.append(row['source_paths'][0])
            summary.update(previous_job_id=previous_id, previous_record_count=len(old_rows),
                changed_record_fields=sorted(fields), changed_source_paths=paths)
        if changed:
            ledger.update(unproductive_actions=0, diagnostic_actions_used=0, stage='review',
                effective_revisions=ledger['effective_revisions'] + 1)
            self.state['document_guard_rejections'] = 0
        else:
            self._unproductive(ledger)
        ledger.update(latest_job_id=result['job_id'], output_fingerprint=fingerprint, last_change=summary)
        ledger['candidates'].append(summary)
        result['revision'] = summary
        return summary

    def decision(self, job_id, review):
        source_id = self.source_for({'job_id': job_id})
        ledger = self.ledger(source_id)
        ledger['issues'] = deepcopy(review.get('issues', []))
        ledger['required_changes'] = deepcopy(review.get('required_changes', []))
        if ledger['stage'] not in {'diagnose', 'blocked'}:
            ledger['stage'] = 'export' if review.get('accepted') else 'review_pending' if review.get('status') == 'review_inconclusive' else 'revise'

    def after(self, name, arguments, result):
        if name == 'document_open':
            self.after_access('document_open', arguments['url'], result)
        source_id = result.get('source_id') or self.source_for(arguments)
        if not source_id:
            return
        self.state['active_document_source_id'] = source_id
        self.state.pop('pending_document_url', None)
        ledger = self.ledger(source_id)
        if name == 'document_decode' and arguments['source_id'] != source_id:
            self.ledger(arguments['source_id']).update(stage='derived', derived_source_id=source_id)
        if result.get('status') == 'exported' and result.get('output_path'):
            ledger.update(stage='completed', output_path=result['output_path'], record_count=result['record_count'])
            self.state['document_guard_rejections'] = 0
        elif name in {'document_inspect', 'document_review'}:
            identity = digest({'name': name, 'arguments': arguments})
            if identity in ledger['observations']:
                self._unproductive(ledger)
            else:
                ledger['observations'][identity] = {'step': self.state.get('step'), 'name': name, 'arguments': deepcopy(arguments)}
        result['recovery'] = self.view(ledger)

    def failure(self, arguments, message):
        if arguments.get('url'):
            target = self.target(arguments['url'])
            target.update(stage='access_gap', last_failure={'message': message[:1000]})
            target['failures'] += 1
        source_id = self.source_for(arguments)
        if source_id:
            ledger = self.ledger(source_id)
            ledger['last_failure'] = message[:1000]
            self._unproductive(ledger)

    def auxiliary(self, action):
        if action.get('name') not in {'history_read', 'history_search', 'reflect'}:
            return
        source_id = self.state.get('active_document_source_id')
        if source_id:
            ledger = self.ledger(source_id)
            if ledger['stage'] in {'revise', 'diagnose'}:
                self._unproductive(ledger)

    @staticmethod
    def target_key(url):
        parts = urlsplit(url)
        return digest(urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, '')))

    def target(self, url):
        key = self.target_key(url)
        key = self.state.get('document_target_aliases', {}).get(key, key)
        return self.state.setdefault('document_targets', {}).setdefault(key, {'target_id': key,
            'url': url, 'stage': 'retrieve', 'attempts': 0, 'failures': 0, 'operations': {}, 'limits': self.limits})

    def before_access(self, name, url):
        target = self.target(url)
        operation = name
        reason = None
        if name == 'document_open' and target.get('source_id'):
            _, meta = self.service._source(target['source_id'])
            self.state['active_document_source_id'] = target['source_id']
            self.state.pop('pending_document_url', None)
            return {'ok': True, 'status': 'source_already_saved', 'source_id': target['source_id'], **meta,
                'retrieval': deepcopy(target), 'message': 'This task already saved and verified this full source. Use document_inspect with its source_id; no new network request was made.'}
        if name == 'download' and target.get('download_path'):
            path = Path(target['download_path'])
            if path.is_file() and path.stat().st_size == target.get('download_bytes'):
                reason = 'This URL already produced a saved download. Inspect/import that artifact with a supported capability; changing the filename is not new evidence.'
        if target.get('next_retry_at', 0) > time.time():
            reason = 'The server rate limited this source. Respect next_retry_at; wait within the task deadline or handle other sources. Changing the access method does not bypass this wait.'
        elif target['stage'] == 'blocked':
            reason = target.get('limitation', 'This target exhausted its retrieval budget.')
        elif name == 'document_open' and target.get('last_failure', {}).get('status') == 'unsupported_content':
            reason = 'The saved response requires an unsupported representation. Reopening the same URL cannot add a parser/import capability. Use an appropriate supported representation or report this capability gap.'
        elif target['attempts'] >= self.limits['max_retrieval_attempts']:
            target.update(stage='blocked', limitation='Bounded retrieval attempts were exhausted across HTTP/browser download methods.')
            reason = target['limitation']
        if reason:
            self.state['document_guard_rejections'] = self.state.get('document_guard_rejections', 0) + 1
            return {'ok': False, 'status': 'retrieval_action_required', 'errorType': 'document_recovery_guard',
                'target_id': target['target_id'], 'url': url, 'message': reason,
                'next_retry_at': target.get('next_retry_at'), 'retrieval': deepcopy(target),
                'effects': 'Requested access was not executed.'}
        target['attempts'] += 1
        target['operations'][operation] = target['operations'].get(operation, 0) + 1
        return None

    def after_access(self, name, url, result):
        target = self.target(url)
        if result.get('url'):
            self.state.setdefault('document_target_aliases', {})[self.target_key(result['url'])] = target['target_id']
        if result.get('source_id'):
            target.update(stage='fetched', source_id=result['source_id'])
        elif name == 'download' and result.get('ok') and result.get('path'):
            path = Path(result['path'])
            if path.is_file():
                target.update(stage='downloaded', download_path=str(path), download_bytes=path.stat().st_size)
        else:
            target['failures'] += 1
            target['last_failure'] = {k: result[k] for k in ('status', 'errorType', 'http_status', 'message', 'needs_browser', 'retry_after') if k in result}
            target['stage'] = 'representation_gap' if result.get('status') == 'unsupported_content' else 'access_gap'
            if result.get('http_status') == 429:
                retry = result.get('retry_after') or '60'
                try:
                    seconds = max(0, float(retry))
                    if not math.isfinite(seconds):
                        raise ValueError('Invalid Retry-After')
                    until = time.time() + seconds
                except (ValueError, TypeError):
                    try:
                        until = parsedate_to_datetime(retry).timestamp()
                    except (ValueError, TypeError):
                        until = time.time() + 60
                target.update(stage='rate_limited', next_retry_at=until)
        result['retrieval'] = deepcopy(target)

    @staticmethod
    def view(ledger):
        fields = ('source_id', 'stage', 'latest_job_id', 'revisions', 'effective_revisions', 'inspections',
            'unproductive_actions', 'diagnostic_actions_used', 'limits', 'last_change', 'issues', 'required_changes', 'limitation', 'last_failure')
        return {k: deepcopy(ledger[k]) for k in fields if k in ledger} | {
            'next_step': {'inspect': 'Inspect observed structure, then declare a candidate.',
                'review': 'Await or refresh the candidate review.', 'revise': 'Locate cited evidence, change the recipe, compare output changes, then review.',
                'review_pending': 'No valid verdict exists. If the saved transient review is retryable, use document_review_retry with its exact review_id within the attempt budget. Otherwise preserve the unapproved candidate and report the review limitation.',
                'diagnose': 'Use the remaining bounded diagnostics for a different query/representation or an effective revision; otherwise move to another source.',
                'export': 'Export the accepted current candidate.', 'completed': 'Keep the delivery summary; use IDs to recover full evidence.',
                'derived': 'Process the source-bound decoded document; preserve this parent as provenance.',
                'blocked': 'Preserve checkpoint, process other pending sources and report the precise limitation.'}[ledger['stage']]}
