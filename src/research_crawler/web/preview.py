"""Bounded source-derived link previews over the application's shared transport."""
import asyncio
import copy
import hashlib
from html.parser import HTMLParser
import math
import re
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import httpx

from ..core.jobs import BudgetExceeded
from ..core.sessions import controlled, open_http
from ..discovery.source_algorithms import SourceAlgorithms, _parse_head
from .cache import utc_now
from .document import normalize_url
from .preview_algorithms import PreviewAlgorithms
from .scraping_models import Link, Links


class _HeadBoundary(HTMLParser):
    """Find markup boundaries without treating script/comment literals as tags."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.source = ''
        self.boundary = None
        self.scope = None

    def append(self, data):
        # Latin-1 maps one byte to one character so parser offsets remain exact
        # byte offsets. Actual page text is decoded with its response encoding.
        text = bytes(data).decode('latin-1')
        self.source += text
        self.feed(text)

    def _offset(self):
        line, column = self.getpos()
        return sum(len(part) + 1 for part in self.source.split('\n')[:line-1]) + column

    def handle_endtag(self, tag):
        if tag == 'head' and self.boundary is None:
            start = self._offset()
            self.boundary = self.source.index('>', start) + 1
            self.scope = 'head'

    def handle_starttag(self, tag, attrs):
        if tag == 'body' and self.boundary is None:
            self.boundary = self._offset()
            self.scope = 'prefix_before_body'


def settings(value):
    if not isinstance(value, dict):
        raise ValueError('link_preview_config must be an object')
    defaults = {'include_internal':True,'include_external':False,'include_patterns':[],
                'exclude_patterns':[],'concurrency':2,'timeout':5,'max_links':20,
                'query':None,'score_threshold':None,'verbose':False,'max_head_bytes':65536}
    if set(value)-set(defaults):
        raise ValueError('Unknown link preview configuration')
    config = {**defaults, **value}
    for key in ('include_internal','include_external','verbose'):
        if type(config[key]) is not bool:
            raise ValueError('Link preview switches must be boolean')
    if not config['include_internal'] and not config['include_external']:
        raise ValueError('Select internal or external link previews')
    for key,lower,upper in [('concurrency',1,8),('max_links',0,200),('max_head_bytes',1024,1000000)]:
        if type(config[key]) is not int or not lower <= config[key] <= upper:
            raise ValueError('Invalid link preview resource limit')
    for key in ('include_patterns','exclude_patterns'):
        if config[key] is None:
            config[key] = []
        if not isinstance(config[key],list) or len(config[key])>30 or any(not isinstance(p,str) or len(p)>500 for p in config[key]):
            raise ValueError('Link preview patterns must be bounded strings')
    if type(config['timeout']) not in (int,float) or not math.isfinite(config['timeout']) or not .1<=config['timeout']<=60:
        raise ValueError('Link preview timeout must be .1..60 seconds')
    if config['query'] is not None and (not isinstance(config['query'],str) or len(config['query'])>500):
        raise ValueError('Invalid preview query')
    if config['score_threshold'] is not None and (type(config['score_threshold']) not in (int,float) or not 0<=config['score_threshold']<=1):
        raise ValueError('score_threshold must be 0..1')
    return SimpleNamespace(**config)


async def preview_links(context, payload, value, *, transport=None):
    config = settings(value)
    algorithm = PreviewAlgorithms()
    original = payload.get('links', [])
    links = Links(internal=[Link(**link) for link in original if not link.get('external')],
                  external=[Link(**link) for link in original if link.get('external')])
    unlimited = SimpleNamespace(**{**vars(config), 'max_links':0})
    eligible = algorithm._filter_links(links, unlimited)
    selected = algorithm._filter_links(links, config)[:200]
    omitted = max(0,len(eligible)-len(selected))
    client = await open_http(context, platform='web', account=context.params.get('account'),
        headers={'User-Agent':'ResearchCrawler/0.1 (local link preview)'}, transport=transport)
    # Header credentials from the parent fetch are never forwarded to automatically
    # discovered hosts. Selected-account cookies retain shared domain scoping.
    slot = asyncio.Semaphore(config.concurrency)
    locks = getattr(context,'_preview_host_locks',{})
    context._preview_host_locks = locks

    async def fetch(url):
        result = {'original_url':url, 'url':url, 'status':'error','head_data':{},
                  'source_page_fetched':False, 'fetched_at':utc_now()}
        try:
            async with slot, asyncio.timeout(config.timeout):
                current = normalize_url(url)
                for redirect in range(6):
                    await context.check_control()
                    if not config.include_external and urlsplit(current).netloc != urlsplit(payload['final_url']).netloc:
                        raise ValueError('external_redirect_excluded')
                    lock = locks.setdefault(urlsplit(current).netloc, asyncio.Lock())
                    async with lock, client.stream('GET', current, headers={'Range':f'bytes=0-{config.max_head_bytes-1}'}) as response:
                        result.update(url=str(response.url),http_status=response.status_code)
                        if response.status_code in (301,302,303,307,308):
                            current = normalize_url(urljoin(str(response.url),response.headers.get('location','')))
                            continue
                        if response.status_code not in (200,206):
                            result['error'] = 'http_'+str(response.status_code)
                            return result
                        content_type = response.headers.get('content-type','').lower()
                        if content_type and not any(t in content_type for t in ('text/html','application/xhtml')):
                            result['error'] = 'unsupported_head_content_type'
                            return result
                        data = bytearray()
                        boundary_parser = _HeadBoundary()
                        complete = False
                        scope = 'head_prefix'
                        async for part in response.aiter_bytes():
                            captured = part[:max(0,config.max_head_bytes-len(data))]
                            data.extend(captured)
                            boundary_parser.append(captured)
                            if boundary_parser.boundary is not None:
                                del data[boundary_parser.boundary:]
                                complete = True
                                scope = boundary_parser.scope
                                break
                            if len(data)>=config.max_head_bytes:
                                break
                        else:
                            # EOF of a range is not EOF of the whole page unless
                            # Content-Range proves the returned slice includes it.
                            ranged = response.headers.get('content-range','')
                            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',ranged)
                            complete = response.status_code==200 or bool(match and int(match[2])+1==int(match[3]))
                            scope = 'response_prefix_at_eof'
                        text = bytes(data).decode(response.encoding or 'utf-8',errors='replace')
                        result.update(head_data=_parse_head(text),head_html=text,
                            head_sha256=hashlib.sha256(bytes(data)).hexdigest(),captured_bytes=len(data),
                            head_encoding=response.encoding or 'utf-8',hash_scope='captured decoded-transfer bytes before text decoding',
                            status='valid' if complete else 'partial',
                            coverage={'complete':complete,'scope':scope,'full_page':False})
                        if not complete:
                            result['error']='head_byte_or_range_limit'
                        return result
                result['error']='redirect_limit'
        except BudgetExceeded as error:
            result['error']=error.reason
        except (httpx.HTTPError,TimeoutError,ValueError) as error:
            result['error']=str(error) if isinstance(error,ValueError) else type(error).__name__
        return result

    async def checked(url):
        try:
            return await controlled(context,fetch(url))
        except BudgetExceeded as error:
            return {'original_url':url,'url':url,'status':'partial','error':error.reason,'head_data':{},'source_page_fetched':False}
    tasks = [asyncio.create_task(checked(url)) for url in selected]
    try:
        results = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
    if config.query and results:
        scorer=SourceAlgorithms()
        scores=scorer._calculate_bm25_score(config.query,[scorer._extract_text_context(item['head_data']) for item in results])
        for item,score in zip(results,scores):
            item['relevance_score']=float(score)
            item['passes_score_threshold']=config.score_threshold is None or score>=config.score_threshold
    merged=algorithm._merge_head_data(links,results,SimpleNamespace(link_preview_config=config,score_links=context.params.get('score_links',False)))
    merged_by_url={link.href:link.model_dump() for group in (merged.internal,merged.external) for link in group}
    output=copy.deepcopy(original)
    for link in output:
        for key,value in merged_by_url.get(link['href'],{}).items():
            if key in {'head_data','head_extraction_status','head_extraction_error','contextual_score','total_score'}:
                link[key]=value
    complete = not omitted and all(item['status']=='valid' for item in results)
    return {'status':'complete' if complete else 'partial','links':output,'results':results,
            'coverage':{'complete':complete,'scope':'selected_link_heads','eligible':len(eligible),
                        'selected':len(selected),'omitted_by_limit':omitted,'full_pages_fetched':False},
            'cache_policy':'fresh preview requests, independent of parent page cache',
            'scoring':'source BM25 whitespace tokens and original total-score combination'}
