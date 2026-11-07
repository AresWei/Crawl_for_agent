"""Homepage protocols observed in Bilibili's public 2026-11-07 JS and responses.

WBI/normalization reuse the attributed MediaCrawler-derived bilibili module.
No private Pro source; protocol provenance: docs/audits/platform-homefeed-current.md.
"""
from dataclasses import replace
from hashlib import sha256
from urllib.parse import urlsplit

from ..core.jobs import NeedsLogin
from ..core.records import Record
from ..core.sessions import open_http
from .bilibili import BilibiliSign, normalize_video
from .discovery_common import DiscoveryClient, DiscoveryUnavailable, schema_error
from .discovery_feed import collect_pages

HOST = 'https://api.bilibili.com'
PATHS = {'trending': '/x/web-interface/wbi/search/square',
         'homefeed': '/x/web-interface/wbi/index/top/feed/rcmd'}


async def collect(context, *, transport=None, interval=1.0):
    mode = context.params['mode']
    if mode not in PATHS:
        raise ValueError('Bilibili homepage mode must be trending or homefeed')
    http = client = signer = None

    async def fetch(cursor):
        nonlocal http, client, signer
        if http is None:
            http = await open_http(context, platform='bili', account=context.params.get('account'),
                transport=transport, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
            client = DiscoveryClient(http, context, 'bili', interval=interval)
        if signer is None:
            body, evidence = await client.get_json(HOST + '/x/web-interface/nav', {})
            data = body.get('data') if isinstance(body, dict) else None
            keys = data.get('wbi_img') if isinstance(data, dict) else None
            if not isinstance(keys, dict) or not all(isinstance(keys.get(k), str) for k in ('img_url', 'sub_url')):
                raise schema_error(evidence, 'wbi_keys_unavailable')
            values = [urlsplit(keys[k]).path.rsplit('/', 1)[-1].split('.')[0] for k in ('img_url', 'sub_url')]
            if any(len(value) != 32 for value in values):
                raise schema_error(evidence, 'invalid_wbi_keys')
            signer = BilibiliSign(*values)
        index = cursor['index']
        params = {'limit': 10, 'platform': 'web'} if mode == 'trending' else {
            'web_location': 1430650, 'y_num': 3, 'fresh_type': 0 if index == 0 else 4,
            'feed_version': 'V8', 'fresh_idx_1h': index, 'fetch_row': 1 + index * 3,
            'fresh_idx': index, 'brush': 0, 'device': 'win', 'homepage_ver': 1,
            'ps': 10, 'last_y_num': 3, 'screen': '1920-1080', 'tt_exp': 0,
            'last_showlist': ';'.join(cursor['shown'])}
        body, evidence = await client.get_json(HOST + PATHS[mode], signer.sign(params))
        # Do not persist ephemeral signing material in evidence URLs.
        evidence['url'] = HOST + PATHS[mode]
        if not isinstance(body, dict):
            raise schema_error(evidence)
        if body.get('code') in (-101, -111):
            raise NeedsLogin('bili', context.params.get('account') or 'anonymous')
        if body.get('code') != 0:
            raise DiscoveryUnavailable('blocked' if body.get('code') in (-352, -412) else 'api_error',
                dict(evidence, api_code=body.get('code')))
        data = body.get('data')
        group = data.get('trending') if isinstance(data, dict) and mode == 'trending' else data
        items = group.get('list' if mode == 'trending' else 'item') if isinstance(group, dict) else None
        if not isinstance(items, list) or len(items) > 100:
            raise schema_error(evidence)
        rows, unsupported, shown = [], 0, []
        for rank, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise schema_error(evidence)
            coverage = {'endpoint': PATHS[mode], 'evidence': evidence, 'rank_at_observation': index * 10 + rank,
                'page_position': rank, 'page_index': index, 'detail_collected': False,
                'feed_kind': 'search_trending_topics' if mode == 'trending' else 'homepage_recommendations',
                'comments_collected': False, 'media_downloaded': False}
            if mode == 'trending':
                word = item.get('keyword')
                if not isinstance(word, str) or not word.strip():
                    raise schema_error(evidence)
                rows.append(Record('bili', 'topic', sha256(word.encode()).hexdigest(), 'https://www.bilibili.com/',
                    {'title': item.get('show_name') or word, 'keyword': word, 'raw_topic': item},
                    observed_at=evidence['observed_at'], coverage=dict(coverage,
                        url_source_field='homepage_fallback', interaction_semantics='heat_score is provider raw metric, not views or comments')))
            else:
                if item.get('goto') != 'av':
                    unsupported += 1
                    continue
                if not item.get('id') or not isinstance(item.get('title'), str):
                    raise schema_error(evidence)
                record = normalize_video({'View': dict(item, aid=item['id'])})
                uri = item.get('uri', '')
                parsed = urlsplit(uri)
                if parsed.scheme != 'https' or parsed.hostname != 'www.bilibili.com' or not parsed.path.startswith('/video/'):
                    raise schema_error(evidence, 'invalid_video_url')
                record.payload.update(title=item['title'], video_url=uri, recommendation_reason=item.get('rcmd_reason'))
                rows.append(replace(record, url=uri, observed_at=evidence['observed_at'],
                    source_date_field='data.item[].pubdate', coverage=dict(record.coverage, **coverage)))
                shown.append('av_n_' + str(item['id']))
        next_cursor = {'index': index + 1, 'shown': (cursor['shown'] + [','.join(shown)])[-20:]}
        return rows, next_cursor, mode == 'trending', evidence, unsupported

    return await collect_pages(context, 'bili', mode, fetch, initial={'index': 0, 'shown': []},
        scope='one_observed_search_trending_snapshot' if mode == 'trending' else 'bounded_homepage_recommendations_no_end_marker')
