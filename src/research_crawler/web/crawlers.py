"""Source-derived crawler registry and Google example, within the unified pipeline.

Adapted from Crawl4AI hub.py and crawlers/google_search/crawler.py at
838869bdda93f25e6d50b3f50ae9577d9b19a586 (Apache-2.0 and NOTICE retained).
Amazon upstream is a literal test stub; actual source extraction below is new.
No crawler here creates a browser, HTTP client, job, or remote model provider.
"""
from __future__ import annotations

from abc import ABC,abstractmethod
import hashlib
import inspect
import json
from pathlib import Path
from urllib.parse import urlencode,urljoin

from bs4 import BeautifulSoup

from ..search.remote import challenge_reason
from .schema_inference import infer_schema,validate_extract


PROFILES={
    'google_search':{'version':'1.0.0','description':'Google text/image source extraction','source_example':True,
                     'expected_hosts':['www.google.com','google.com'],'rate_limit_documentation':'10 RPM; not a verified provider allowance'},
    'amazon_product':{'version':'local-1','description':'Actual product DOM/JSON-LD extraction','source_example':'upstream literal stub replaced',
                      'expected_hosts':['www.amazon.com','amazon.com']},
}
TARGETS={
    'organic_schema':({'title':'','link':'','snippet':'','date':''},'organic search results; title, link URL from href, snippet and date'),
    'top_stories_schema':({'title':'','link':'','source':'','date':''},'Top Stories; title, link URL from href, publisher/source, date and imageUrl from the source image'),
    'suggested_query_schema':({'query':''},'People also search for; suggested query text only'),
}
MARKER='research-crawler-google-images'


class BaseCrawler(ABC):
    """Upstream async interface; an existing acquired source is now explicit."""
    @abstractmethod
    async def run(self,url='',**kwargs):pass

    def __init_subclass__(cls,**kwargs):
        super().__init_subclass__(**kwargs)
        if not inspect.iscoroutinefunction(cls.run) or len(inspect.signature(cls.run).parameters)<2:
            raise TypeError('Crawler run must be async and accept self plus url')


class GoogleSearchCrawler(BaseCrawler):
    meta=PROFILES['google_search']
    async def run(self,url='',*,payload,context=None,config=None,identity_scope=None,model=None):
        return await enrich_crawler({**payload,'final_url':url or payload.get('final_url','')},
            {**(config or {}),'name':'google_search'},context=context,identity_scope=identity_scope,model=model)


class AmazonProductCrawler(BaseCrawler):
    meta=PROFILES['amazon_product']
    async def run(self,url='',*,payload,context=None,config=None,identity_scope=None,model=None):
        return await enrich_crawler({**payload,'final_url':url or payload.get('final_url','')},
            {**(config or {}),'name':'amazon_product'},context=context,identity_scope=identity_scope,model=model)


class CrawlerHub:
    # Fixed registry preserves get(name) without importing job-controlled module paths.
    _crawlers={'google_search':GoogleSearchCrawler,'amazon_product':AmazonProductCrawler}

    @classmethod
    def get(cls,name):return cls._crawlers.get(name)


def list_crawlers():
    return [{'name':name,**metadata} for name,metadata in PROFILES.items()]


def _config(config):
    allowed={'name','query','search_type','page_start','page_length','max_records','sections','schemas','schema_options'}
    if not isinstance(config,dict) or set(config)-allowed:raise ValueError('Unsupported crawler configuration')
    if config.get('name') not in PROFILES:raise ValueError('Unknown registered crawler')
    result=dict(config)
    for key,default,maximum in [('max_records',20,100),('page_start',1,100),('page_length',10,100)]:
        value=result.setdefault(key,default)
        if type(value) is not int or not 1<=value<=maximum:raise ValueError('Invalid crawler '+key)
    query=result.get('query','')
    if not isinstance(query,str) or len(query)>1000:raise ValueError('crawler.query must be bounded text')
    if result.get('search_type','text') not in {'text','image'}:raise ValueError('Invalid crawler search_type')
    sections=result.get('sections',list(TARGETS))
    if not isinstance(sections,list) or not sections or len(sections)>3 or any(key not in TARGETS for key in sections):
        raise ValueError('Invalid Google text sections')
    if 'schemas' in result and (not isinstance(result['schemas'],dict) or set(result['schemas'])-set(TARGETS)):
        raise ValueError('schemas requires known Google text section names')
    options=result.get('schema_options',{})
    if not isinstance(options,dict) or set(options)-{'max_attempts','max_tokens','max_html_chars','max_source_chars','max_seconds','cache_mode','cache_ttl_seconds','max_calls','max_total_tokens'}:
        raise ValueError('Invalid crawler schema_options')
    return result


def prepare_crawler_params(params):
    if params.get('crawler') is None:return params
    config=_config(params['crawler']);result={**params,'crawler':config}
    if config['name']=='google_search':
        if not any(params.get(key) for key in ('urls','local_files','raw_html')):
            if not config.get('query','').strip():raise ValueError('Google crawler requires query or explicit source')
            query={'q':config['query'],'gl':'sg','hl':'en'}
            if config.get('search_type')=='image':query.update(tbs='qdr:d',udm=2)
            # Preserve the frozen example's offset convention, explicitly documented.
            if config['page_start']>1:query['start']=config['page_start']*10
            if config['page_length']>1:query['num']=config['page_length']
            result['urls']=['https://www.google.com/search?'+urlencode(query)]
        if config.get('search_type')=='image':
            script=(Path(__file__).parent/'crawler_assets/google_images.js').read_text(encoding='utf-8')
            script=script.replace('__MAX_RESULTS__',str(config['max_records']))
            existing=result.get('js_code',[])
            if not isinstance(existing,list):existing=[existing]
            result['js_code']=[*existing,{'code':script,'mode':'body'}]
            result['render']='browser'
    return result


def _product(html,url,limit):
    """Independently authored actual extraction; never return upstream fake product."""
    soup=BeautifulSoup(html,'lxml');products=[];truncated=False
    title=soup.select_one('#productTitle');price=soup.select_one('#corePrice_feature_div .a-offscreen, #priceblock_ourprice, #priceblock_dealprice, .priceToPay .a-offscreen')
    if title and title.get_text(' ',strip=True):
        value={'name':title.get_text(' ',strip=True),'url':url}
        if price:value['price']=price.get_text(' ',strip=True)
        products.append({'product':value,'source':{'kind':'dom','selectors':{'name':'#productTitle','price':'#corePrice_feature_div .a-offscreen, #priceblock_ourprice, #priceblock_dealprice, .priceToPay .a-offscreen'}}})
    for index,node in enumerate(soup.select('script[type="application/ld+json"]')[:50]):
        text=node.string or node.get_text()
        if len(text)>200000:truncated=True;continue
        try:data=json.loads(text)
        except (ValueError,TypeError):continue
        pending=[data];visited=0
        while pending and visited<1000:
            item=pending.pop();visited+=1
            if isinstance(item,list):pending.extend(item);continue
            if not isinstance(item,dict):continue
            kinds=item.get('@type',[]);kinds=[kinds] if isinstance(kinds,str) else kinds if isinstance(kinds,list) else []
            if 'Product' in kinds and isinstance(item.get('name'),str):
                value={'name':item['name']}
                for key in ('sku','description','image','url','offers','brand'):
                    if key in item:value[key]=item[key]
                products.append({'product':value,'source':{'kind':'json_ld','script_index':index,'script_sha256':hashlib.sha256(text.encode()).hexdigest()}})
            if '@graph' in item:pending.append(item['@graph'])
            if len(products)>limit:truncated=True;break
        if pending:truncated=True
        if len(products)>limit:break
    return products[:limit],truncated


async def enrich_crawler(payload,config,*,context=None,identity_scope=None,model=None):
    config=_config(config);name=config['name'];html=payload.get('html','');url=payload.get('final_url',payload.get('requested_url',''))
    result={'name':name,'status':'partial','records':[],'sections':{},'source':{'url':url,'field':'html','sha256':hashlib.sha256(html.encode()).hexdigest()},
            'full_provider_coverage':False,'semantic_accuracy_automatically_proven':False,'limits':[]}
    if len(html)>2_000_000:result['limits'].append('crawler_source_budget');return result
    reason=challenge_reason(html)
    if reason:result['limits'].append(reason);return result
    if name=='amazon_product':
        result['records'],truncated=_product(html,url,config['max_records'])
        result['implementation']='independent_actual_source_extraction; upstream example was a literal stub'
        if truncated:result['limits'].append('crawler_record_or_json_budget')
        if not result['records']:result['limits'].append('product_fields_not_found')
    elif config.get('search_type','text')=='image':
        soup=BeautifulSoup(html,'lxml');node=soup.find('script',id=MARKER)
        result['source']['field']='captured_dom_with_explicit_derived_image_marker'
        result['derived_from']='page window.W_jd and matching data-docid DOM; source-derived browser script'
        if node:
            try:document=json.loads(node.string or node.get_text())
            except (ValueError,TypeError):document={}
            records=document.get('records',[])
            if isinstance(records,list):result['records']=records[:config['max_records']]
            if document.get('limited'):result['limits'].append('crawler_image_budget')
            if not document.get('available'):result['limits'].append('google_image_runtime_data_unavailable')
        else:result['limits'].append('google_image_script_result_missing')
        if not result['records']:result['limits'].append('image_results_not_found')
    else:
        for key in dict.fromkeys(config.get('sections',list(TARGETS))):
            target,query=TARGETS[key]
            schema=config.get('schemas',{}).get(key)
            if schema is not None:
                records,diagnostic=validate_extract(schema,html,'css',max_records=config['max_records'])
                section={'schema':schema,'records':records,'status':'complete' if diagnostic['success'] else 'partial',
                         'diagnostic':diagnostic,'records_origin':'deterministic_original_html_extraction','schema_origin':'explicit_user_schema'}
            else:
                options={'selector_type':'css','query':'Google search source: extract '+query+'. Generate source selectors only; do not invent missing fields.',
                    'target_json':target,'max_records':config['max_records'],'max_calls':6,'max_total_tokens':6144,
                    'identity_scope':identity_scope,**config.get('schema_options',{})}
                section=await infer_schema(html,url,options,context=context,model=model)
            result['sections'][key]=section
            result['records'].extend({'section':key,**row} for row in section['records'])
            if section['status']!='complete':result['limits'].append(key+'_partial')
    if result['records'] and not result['limits']:result['status']='complete'
    return result
