"""Bounded listing snapshots: persist unconsumed cards before advancing a feed.

Dynamic recommendations have no inferred end marker. A resumed task consumes
its saved page, while a new task requests a fresh recommendation snapshot.
"""
from dataclasses import asdict

from ..core.jobs import BudgetExceeded
from ..core.records import Record, canonical_json
from .discovery_common import DiscoveryUnavailable, schema_error
from .weibo._shared import budgets


async def collect_pages(context, platform, mode, fetch, *, initial, scope):
    limits = budgets(context.params)
    stream = f'discovery:{platform}:{mode}:snapshots:v1'
    cp = await context.checkpoint(stream)
    reused = cp is not None
    state = (cp or {}).get('cursor') or {'cursor': initial, 'page': None, 'offset': 0,
        'seen': [], 'cursors': [], 'pages': 0, 'unsupported': 0}
    seen = set(state['seen'])
    acquired = 0

    def summary(reason, complete=False, **extra):
        return {'status': 'complete' if complete else 'partial', 'platform': platform,
            'mode': mode, 'scope': scope, 'coverage_complete': complete,
            'coverage': [{'count': len(seen), 'pages': state['pages'], 'complete': complete,
                          'reason': reason, 'unsupported_cards': state['unsupported']}],
            'snapshot_reused': reused, 'network_pages_this_run': acquired,
            'comments_collected': False, 'media_downloaded': False, **extra}

    try:
        while True:
            page = state['page']
            if page and state['offset'] == len(page['records']):
                if page['ended']:
                    return summary('snapshot_end', state['unsupported'] == 0)
                marker = canonical_json(page['next_cursor'])
                if not page['records'] or marker in state['cursors']:
                    return summary('empty_page' if not page['records'] else 'repeated_cursor')
            if len(seen) >= limits['maximum']:
                return summary('record_limit')
            if page is None or state['offset'] == len(page['records']):
                if acquired >= limits['page_limit']:
                    return summary('page_limit')
                if page is not None:
                    state['cursor'] = page['next_cursor']
                records, next_cursor, ended, evidence, unsupported = await fetch(state['cursor'])
                if type(ended) is not bool:
                    raise ValueError('Listing end marker must be explicit')
                page = {'records': [asdict(row) for row in records], 'next_cursor': next_cursor,
                        'ended': ended, 'evidence': evidence}
                if len(canonical_json(page).encode('utf-8')) > 4_194_304:
                    raise schema_error(evidence, 'snapshot_byte_limit')
                state.update(page=page, offset=0, pages=state['pages'] + 1,
                    unsupported=state['unsupported'] + unsupported)
                state['cursors'].append(canonical_json(state['cursor']))
                acquired += 1
            batch = []
            while state['offset'] < len(page['records']) and len(seen) < limits['maximum']:
                row = Record(**page['records'][state['offset']])
                state['offset'] += 1
                if row.key not in seen:
                    batch.append(row)
                    seen.add(row.key)
            state['seen'] = sorted(seen)
            complete = page['ended'] and state['offset'] == len(page['records']) and state['unsupported'] == 0
            report = summary('snapshot_end' if complete else 'record_limit' if len(seen) >= limits['maximum'] else 'page_limit', complete)
            cp = await context.commit_page(stream, batch, next_cursor=state, exhausted=complete,
                expected_revision=cp['revision'] if cp else 0, metadata={'summary': report})
    except DiscoveryUnavailable as error:
        report = summary(error.reason, evidence=error.evidence, access_state=error.reason)
        await context.commit_page(stream, [], next_cursor=state, exhausted=False,
            expected_revision=cp['revision'] if cp else 0, metadata={'summary': report})
        return report
    except BudgetExceeded as error:
        error.summary = summary(error.reason)
        raise
