"""Public, keyless Stack Exchange Q&A through the shared task/HTTP/store core.

Independently authored from the official API2.3 documentation; no account state,
credential, secondary crawler, browser or provider fallback is used.
"""
from __future__ import annotations
import asyncio
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import time
import uuid

import httpx
from bs4 import BeautifulSoup

from ..core.records import Record,utcnow
from ..core.sessions import open_http

BASE='https://api.stackexchange.com/2.3'
CAPABILITIES={'modes':['questions','search','detail','qa'],'site':'explicit API site parameter; default stackoverflow',
 'authentication':'public keyless only','pagination':'page/pagesize/has_more with durable response remainder',
 'answers':True,'comments':'question and answer comments; no threaded reply claim',
 'accepted_answer':'source acceptance flag, not correctness','live_verified':False,
 'limits':['API quota/backoff and one-minute duplicate-request embargo','public returned objects only; not deleted/private content']}


def _int(params,name,default,low,high):
    value=params.get(name,default)
    if type(value) is not int or not low<=value<=high:raise ValueError(f'{name} must be {low}..{high}')
    return value


def settings(params):
    if any(params.get(key) for key in ('account','key','access_token')):raise ValueError('Stack Exchange discovery is public keyless only')
    mode=params.get('mode','questions');site=params.get('site','stackoverflow')
    if mode not in CAPABILITIES['modes']:raise ValueError('Unsupported Stack Exchange mode')
    if not isinstance(site,str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,99}',site):raise ValueError('Invalid API site parameter')
    query=params.get('query','')
    if mode=='search' and (not isinstance(query,str) or not query.strip() or len(query)>1000):raise ValueError('Supply bounded search query')
    ids=params.get('ids',[])
    if mode in {'detail','qa'}:
        if not isinstance(ids,list) or not 1<=len(ids)<=50 or any(not re.fullmatch(r'[1-9][0-9]{0,14}',str(i)) for i in ids):
            raise ValueError('Select1..50 numeric question IDs')
        ids=list(dict.fromkeys(str(i) for i in ids))
    answers=params.get('include_answers',mode in {'detail','qa'});comments=params.get('include_comments',False)
    if type(answers) is not bool or type(comments) is not bool:raise ValueError('include_answers/include_comments must be boolean')
    sort=params.get('sort','relevance' if mode=='search' else 'activity')
    allowed={'activity','creation','votes'}|({'relevance'} if mode=='search' else {'hot','week','month'} if mode=='questions' else set())
    if sort not in allowed:raise ValueError('Unsupported question sort')
    return {'mode':mode,'site':site,'query':query,'ids':ids,'include_answers':answers,'include_comments':comments,'sort':sort,
        'page_size':_int(params,'page_size',20,1,100),'max_pages':_int(params,'max_pages',10,1,100),
        'max_items':_int(params,'max_items',100,1,1000)}


def _policy_path(context):return context.paths.root/'state/stackexchange-throttle.json'
def _policy(context):
    path=_policy_path(context)
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:return {'backoff_until':0,'quota_until':0,'requests':{}}
    if not isinstance(data,dict):raise ValueError('Invalid Stack Exchange throttle state')
    return data
def _save_policy(context,data):
    path=_policy_path(context);temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary.write_text(json.dumps(data,sort_keys=True),encoding='utf-8');temporary.replace(path)


def _record(item,kind,site,evidence,question=None):
    id_key={'question':'question_id','answer':'answer_id','comment':'comment_id'}[kind]
    identity=item.get(id_key)
    if type(identity) is not int or identity<=0:raise ValueError('Invalid Q&A identity')
    body=item.get('body','')
    if not isinstance(body,str):raise ValueError('Invalid Q&A body')
    source_date=None
    if 'creation_date' in item:
        stamp=item['creation_date']
        if type(stamp) is not int or stamp<0:raise ValueError('Invalid source timestamp')
        source_date=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
    payload={**item,'site':site,'entity':kind,'content':BeautifulSoup(body,'html.parser').get_text(' ',strip=True),
        'content_derivation':'plain text from source body HTML','author_attribution':'public API owner object retained',
        'accepted_is_correctness':False}
    if question is not None:payload['parent_question_id']=int(question)
    # Site parameters are not necessarily hostnames (meta sites/custom domains).
    # Retain the actual API source when an object supplies no permalink.
    url=item.get('link') or evidence['url']
    return Record('stackexchange',kind,f'{site}:{identity}',url,payload,source_date=source_date,
        source_date_field='creation_date' if source_date else None,coverage={'scope':'source_returned_public_object','evidence':evidence,
        'body_present':'body' in item,'source_owner_retained_for_attribution':True,'full_discussion_claim':False})


async def collect(context,*,transport=None,interval=1.0):
    cfg=settings(context.params);stream='discovery:stackexchange'
    identity={k:v for k,v in cfg.items() if k not in {'max_pages','max_items'}}
    fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    cp=await context.checkpoint(stream)
    if cp:
        state=cp['cursor']
        if state['config_hash']!=fingerprint:raise ValueError('Q&A checkpoint configuration changed')
    else:
        path='/search/advanced' if cfg['mode']=='search' else '/questions/'+(';'.join(cfg['ids'])) if cfg['mode'] in {'detail','qa'} else '/questions'
        state={'config_hash':fingerprint,'pending':[{'kind':'question','path':path,'page':1}],
               'seen':[],'scheduled':[],'buffer':None,'not_found':[],'returned_questions':[],'quota_remaining':None}
    revision=cp['revision'] if cp else 0;pages=0;reason=None;last_request=0
    http=await open_http(context,platform='stackexchange_public',account=None,transport=transport,
        headers={'User-Agent':'ResearchCrawler/0.1 (public Q&A research)','Accept':'application/json'})
    async def commit(records):
        nonlocal revision
        saved=await context.commit_page(stream,records,next_cursor=state,
            exhausted=not state['pending'] and state['buffer'] is None and not state['not_found'],expected_revision=revision,
            metadata={'reason':reason,'scope':'selected API routes only'})
        revision=saved['revision']
    if cp is None:await commit([])
    def enqueue(kind,path,question):
        key=kind+':'+path
        if key not in state['scheduled']:
            state['scheduled'].append(key);state['pending'].append({'kind':kind,'path':path,'page':1,'question':question})
    while state['pending'] or state['buffer']:
        await context.check_control()
        if len(state['seen'])>=cfg['max_items']:reason='record_limit';break
        if state['buffer'] is None:
            if pages>=cfg['max_pages']:reason='page_limit';break
            work=state['pending'][0];query={'site':cfg['site'],'page':work['page'],'pagesize':cfg['page_size'],'filter':'withbody',
                'sort':cfg['sort'] if work['kind']=='question' else 'votes' if work['kind']=='answer' else 'creation','order':'desc'}
            if cfg['mode']=='search' and work['kind']=='question':query['q']=cfg['query']
            request_key=hashlib.sha256(json.dumps([work['path'],query],sort_keys=True).encode()).hexdigest()
            policy=_policy(context);now=time.time()
            waits=[('server_backoff',policy.get('backoff_until',0)),('quota_exhausted',policy.get('quota_until',0)),
                   ('duplicate_request_embargo',policy.get('requests',{}).get(request_key,0)+60)]
            blocked=next((label for label,until in waits if until>now),None)
            if blocked:reason=blocked;break
            if interval and time.monotonic()-last_request<interval:await asyncio.sleep(interval-(time.monotonic()-last_request))
            await context.check_control();last_request=time.monotonic()
            policy['requests']={key:value for key,value in policy.get('requests',{}).items() if now-value<60}
            policy['requests'][request_key]=now;_save_policy(context,policy)
            try:
                response=await http.get(BASE+work['path'],params=query,follow_redirects=False)
                pages+=1
                content=response.content;digest=hashlib.sha256(content).hexdigest()
                artifact=context.paths.run(context.job_id)/('stackexchange-'+digest+'.json');artifact.parent.mkdir(parents=True,exist_ok=True)
                artifact.write_bytes(content)
                evidence={'url':str(response.request.url),'observed_at':utcnow(),'http_status':response.status_code,
                          'response_sha256':digest,'artifact':str(artifact),'bytes':len(content)}
                retry=response.headers.get('Retry-After')
                if retry:
                    try:until=time.time()+max(0,float(retry))
                    except ValueError:
                        try:until=parsedate_to_datetime(retry).timestamp()
                        except (ValueError,TypeError,OverflowError):until=0
                    if until>time.time():policy['backoff_until']=max(policy.get('backoff_until',0),until);_save_policy(context,policy)
                try:body=response.json()
                except (ValueError,UnicodeError):body=None
                if isinstance(body,dict) and type(body.get('backoff')) is int and 0<body['backoff']<=86400:
                    policy['backoff_until']=max(policy.get('backoff_until',0),time.time()+body['backoff']);_save_policy(context,policy)
                if response.status_code!=200:
                    reason='rate_limited' if response.status_code==429 else 'http_'+str(response.status_code);state['last_error']=evidence;break
                if not isinstance(body,dict):raise ValueError('Expected response wrapper')
                backoff=body.get('backoff',0);quota=body.get('quota_remaining')
                if type(backoff) is not int or not 0<=backoff<=86400:raise ValueError('Invalid backoff')
                if quota is not None and (type(quota) is not int or quota<0):raise ValueError('Invalid quota')
                policy['backoff_until']=max(policy.get('backoff_until',0),time.time()+backoff if backoff else 0)
                if quota==0:policy['quota_until']=time.time()+86400
                _save_policy(context,policy);state['quota_remaining']=quota
                if 'error_id' in body:reason='api_error';state['last_error']={**evidence,'error_id':body['error_id'],'error_name':body.get('error_name')};break
                if not isinstance(body.get('items'),list) or type(body.get('has_more')) is not bool:raise ValueError('Invalid pagination wrapper')
                # Validate whole page before committing any normalized record or advancing.
                records=[_record(item,work['kind'],cfg['site'],evidence,work.get('question')) for item in body['items']]
                page_hash=hashlib.sha256(json.dumps([record.native_id for record in records]).encode()).hexdigest()
                previous=work.setdefault('seen_pages',[])
                if body['has_more'] and (not records or page_hash in previous):
                    reason='empty_or_repeated_page';state['last_error']=evidence;break
                previous.append(page_hash)
                state['buffer']={'work':dict(work),'items':body['items'],'offset':0,'has_more':body['has_more'],'evidence':evidence}
            except (httpx.HTTPError,ValueError,TypeError,OverflowError,OSError) as error:
                reason='request_or_schema_error';state['last_error']={'type':type(error).__name__,'path':work['path'],'page':work['page']};break
        buffered=state['buffer'];work=buffered['work'];rows=[]
        while buffered['offset']<len(buffered['items']) and len(state['seen'])<cfg['max_items']:
            item=buffered['items'][buffered['offset']];buffered['offset']+=1
            record=_record(item,work['kind'],cfg['site'],buffered['evidence'],work.get('question'))
            if record.key in state['seen']:continue
            state['seen'].append(record.key);rows.append(record)
            if work['kind']=='question':
                qid=str(item['question_id'])
                if qid not in state['returned_questions']:state['returned_questions'].append(qid)
                if cfg['include_answers']:enqueue('answer',f'/questions/{qid}/answers',qid)
                if cfg['include_comments']:enqueue('comment',f'/posts/{qid}/comments',qid)
            elif work['kind']=='answer' and cfg['include_comments']:
                enqueue('comment',f'/posts/{item["answer_id"]}/comments',str(item.get('question_id',work.get('question'))))
        if buffered['offset']==len(buffered['items']):
            if buffered['has_more']:
                state['pending'][0]['page']+=1
            else:
                state['pending'].pop(0)
                if work['kind']=='question' and cfg['mode'] in {'detail','qa'}:
                    state['not_found']=[qid for qid in cfg['ids'] if qid not in state['returned_questions']]
            state['buffer']=None
        await commit(rows)
    if state['not_found'] and reason is None:reason='selected_questions_not_returned'
    if reason:await commit([])
    complete=not state['pending'] and state['buffer'] is None and not state['not_found'] and reason is None
    return {'status':'complete' if complete else 'partial','platform':'stackexchange','site':cfg['site'],'mode':cfg['mode'],
        'reason':reason,'records':len(state['seen']),'requests_this_run':pages,'pending_routes':len(state['pending']),
        'buffered_remaining':len(state['buffer']['items'])-state['buffer']['offset'] if state['buffer'] else 0,
        'quota_remaining':state['quota_remaining'],'missing_question_ids':state['not_found'],'last_error':state.get('last_error'),
        'coverage_complete':complete,'scope':'selected public API routes and requested child collections',
        'include_answers':cfg['include_answers'],'include_comments':cfg['include_comments'],'accepted_is_correctness':False,
        'paid_api':False,'authentication':'none','full_site_coverage':False}
