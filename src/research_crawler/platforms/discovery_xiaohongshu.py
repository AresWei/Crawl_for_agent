"""Selected-session homepage cards from the publicly delivered XHS web protocol.

Uses the existing attributed client and normalizer; no independent signing or
network lifecycle. The homepage omits detail text, dates and comment counts.
"""
from dataclasses import replace
from hashlib import sha256

from ..core.records import utcnow
from ..core.sessions import open_http
from .discovery_common import DiscoveryUnavailable, schema_error
from .discovery_feed import collect_pages
from .xiaohongshu import XiaohongshuClient, normalize_note
from .xiaohongshu_support import PlatformError, UA

PATH = '/api/sns/web/v1/homefeed'
CAPABILITIES = {'modes': ['homefeed'], 'feed_kind': 'selected_session_homepage_recommendations',
    'pagination': 'cursor_score + note_index; no explicit terminal marker', 'personalized': True,
    'comments': False, 'media': ['cover_url'], 'requires_login': True,
    'live_verified': 'selected_main_first_page_2026-11-07',
    'limits': ['summary cards only; detail text/comments/dates need detail collection',
               'finite page budget is partial; resume preserves unconsumed cards']}


class HomeClient(XiaohongshuClient):
    async def json(self, method, url, **kwargs):
        response = await self.request(method, url, **kwargs)
        self.evidence = {'url': url, 'observed_at': utcnow(), 'http_status': response.status_code,
            'bytes': len(response.content), 'response_sha256': sha256(response.content).hexdigest()}
        try:
            body = response.json()
        except ValueError:
            raise schema_error(self.evidence, 'non_json_response') from None
        if not isinstance(body, dict):
            raise schema_error(self.evidence)
        return body


async def collect(context, *, transport=None, interval=1.0):
    if context.params.get('mode') != 'homefeed':
        raise ValueError('Xiaohongshu discovery supports homefeed')
    if context.params.get('international'):
        raise ValueError('International homepage protocol has not been verified')
    client = None

    async def fetch(cursor):
        nonlocal client
        if client is None:
            http = await open_http(context, platform='xhs', account=context.params.get('account'),
                transport=transport, headers={'User-Agent': UA, 'Referer': 'https://www.xiaohongshu.com/',
                    'Content-Type': 'application/json;charset=UTF-8'})
            client = HomeClient(http, context, interval=interval)
        values = {'cursor_score': cursor['score'], 'num': 30,
            'refresh_type': 1 if cursor['index'] == 0 else 3, 'note_index': cursor['index'],
            'unread_begin_note_id': '', 'unread_end_note_id': '', 'unread_note_count': 0,
            'category': 'homefeed_recommend', 'search_key': '', 'need_num': 10,
            'image_formats': ['jpg', 'webp', 'avif'], 'need_filter_image': False}
        try:
            data = await client.api(PATH, values, method='POST')
        except PlatformError as error:
            raise DiscoveryUnavailable('api_error', dict(getattr(client, 'evidence', {}), api_code=error.code)) from None
        evidence = client.evidence
        if not isinstance(data.get('items'), list) or not isinstance(data.get('cursor_score'), str) or len(data['items']) > 100:
            raise schema_error(evidence)
        rows, unsupported = [], 0
        for position, item in enumerate(data['items'], 1):
            if not isinstance(item, dict):
                raise schema_error(evidence)
            if item.get('model_type') != 'note':
                unsupported += 1
                continue
            card = item.get('note_card')
            if not item.get('id') or not isinstance(card, dict) or not isinstance(card.get('display_title'), str):
                raise schema_error(evidence)
            note = dict(card, note_id=item['id'], title=card['display_title'],
                xsec_token=item.get('xsec_token', ''), xsec_source='pc_feed')
            record = normalize_note(note)
            cover = card.get('cover') or {}
            record.payload.update(cover_url=cover.get('url_default') or cover.get('url_pre') or '',
                source_fields_present=sorted(card.keys()))
            record.coverage.update(endpoint=PATH, evidence=evidence, feed_kind=CAPABILITIES['feed_kind'],
                rank_at_observation=cursor['index'] + position, page_position=position,
                detail_collected=False, comments_collected=False,
                missing_fields=['detail_text', 'publication_date', 'comment_count', 'collected_count', 'share_count'])
            rows.append(replace(record, observed_at=evidence['observed_at'], source_date=None, source_date_field=None))
        next_cursor = {'score': data['cursor_score'], 'index': cursor['index'] + len(data['items'])}
        # A changed index must not disguise a repeated source cursor.
        if not data['cursor_score'] or data['cursor_score'] == cursor['score']:
            next_cursor = cursor
        return rows, next_cursor, False, evidence, unsupported

    return await collect_pages(context, 'xhs', 'homefeed', fetch, initial={'score': '', 'index': 0},
        scope='bounded_selected_session_homepage_cards_no_end_marker')
