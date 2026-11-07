"""Bounded browser observations in the shared page/job lifecycle.

Event fields follow Crawl4AI async_crawler_strategy.py (Apache-2.0,
838869bdda93f25e6d50b3f50ae9577d9b19a586). Transport ownership, resource
limits, credential redaction and artifact persistence are integrated here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

from ..core.accounts import AccountStore
from ..core.sessions import controlled


SAFE_HEADERS={'content-type','content-length','cache-control','etag','last-modified',
              'date','accept','accept-language','content-encoding','server'}


def public_url(value):
    try:
        parts=urlsplit(value)
        if parts.scheme not in {'http','https'}:
            return parts.scheme+':' if parts.scheme else '[non-network URL]'
        host=parts.hostname or ''
        if ':' in host:host='['+host+']'
        if parts.port:host+=':'+str(parts.port)
        return urlunsplit((parts.scheme,host,parts.path,'[redacted]' if parts.query else '', ''))
    except ValueError:
        return '[invalid URL]'


class BrowserEvidence:
    def __init__(self,context,page,*,adapter=None):
        self.context,self.page=context,page
        self.params=context.params
        self.network=bool(self.params.get('capture_network_requests') or self.params.get('capture_response_bodies'))
        self.console=bool(self.params.get('capture_console_messages'))
        self.tls=bool(self.params.get('fetch_ssl_certificate'))
        self.limit=self._limit('max_capture_events',500,1,5000)
        self.byte_limit=self._limit('max_capture_bytes',1_000_000,1024,10_000_000)
        self.body_limit=self._limit('max_capture_body_bytes',65536,0,1_000_000)
        self.events=[];self.listeners=[];self.tasks=set();self.pending_requests=set()
        self.bytes=0;self.omitted=0;self.body_omitted=0;self.errors=0
        self.stopped=False;self.closed=False;self.final=None
        self.tls_session=None
        self.adapter=adapter
        self.adapter_console=bool(adapter and self.params.get('browser_provider')=='patchright' and self.console)
        state=AccountStore(context.paths).state('web',self.params.get('account')) or {}
        self.secrets={str(cookie.get('value','')) for cookie in state.get('cookies',[])}
        self.secrets.update(str(item.get('value','')) for origin in state.get('origins',[]) for item in origin.get('localStorage',[]))
        self.secrets.update(str(value) for key,value in self.params.get('headers',{}).items()
                            if key.lower() in {'authorization','cookie','x-api-key'})
        self.secrets={value for value in self.secrets if len(value)>=6}

    def _limit(self,key,default,low,high):
        value=self.params.get(key,default)
        if type(value) is not int or not low<=value<=high:raise ValueError('Invalid '+key)
        return value

    def scrub(self,value):
        value=str(value)
        for secret in self.secrets:value=value.replace(secret,'[redacted]')
        value=re.sub(r'https?://[^\s<>"\x27]+',lambda match:public_url(match[0]),value)
        value=re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*',r'\1[redacted]',value)
        value=re.sub(r'(?i)((?:password|passwd|token|api[_-]?key|secret|cookie|authorization)["\x27]?\s*[:=]\s*["\x27]?)[^\s,;"\x27}]+',r'\1[redacted]',value)
        return value

    def emit(self,event):
        if self.stopped:return
        event={'timestamp':time.time(),**event}
        encoded=json.dumps(event,ensure_ascii=False,separators=(',',':')).encode()
        if len(self.events)>=self.limit or self.bytes+len(encoded)>self.byte_limit:
            self.omitted+=1;return
        self.events.append(event);self.bytes+=len(encoded)

    def listen(self,event,handler):
        self.page.on(event,handler);self.listeners.append((event,handler))

    async def start(self):
        self.context.resources.push_async_callback(self.close)
        if self.tls:
            self.tls_session=await self.page.context.new_cdp_session(self.page)
            await self.tls_session.send('Network.enable')
        if self.network:
            self.listen('request',self.request)
            self.listen('response',self.response)
            self.listen('requestfinished',self.finished)
            self.listen('requestfailed',self.failed)
        if self.adapter_console:
            if self.params.get('cdp_url'):
                raise ValueError('Patchright console instrumentation currently requires an owned page')
            await self.adapter.setup_console_capture(self.page,[])
            await self.adapter.setup_error_capture(self.page,[])
        elif self.console:
            self.listen('console',lambda msg:self.emit({'event_type':'console','type':msg.type,
                'text':self.scrub(msg.text[:8192]),'text_truncated':len(msg.text)>8192}))
            self.listen('pageerror',lambda error:self.emit({'event_type':'pageerror','text':self.scrub(str(error)[:8192])}))
        return self

    def request(self,request):
        self.pending_requests.add(request)
        self.emit({'event_type':'request','url':self.scrub(public_url(request.url)),'method':request.method,
            'resource_type':request.resource_type,'is_navigation_request':request.is_navigation_request(),
            'headers':{key:self.scrub(value) for key,value in request.headers.items() if key.lower() in SAFE_HEADERS},
            'post_data':'[not retained: request payload may contain credentials]' if request.method not in {'GET','HEAD'} else None})

    def response(self,response):
        self.emit({'event_type':'response','url':self.scrub(public_url(response.url)),'status':response.status,
            'status_text':response.status_text,'from_service_worker':response.from_service_worker,
            'headers':{key:self.scrub(value) for key,value in response.headers.items() if key.lower() in SAFE_HEADERS},
            'request_timing':response.request.timing})

    def failed(self,request):
        self.pending_requests.discard(request)
        self.emit({'event_type':'request_failed','url':self.scrub(public_url(request.url)),
            'method':request.method,'failure_text':self.scrub(request.failure or 'Unknown failure')})

    def finished(self,request):
        self.pending_requests.discard(request)
        if not self.params.get('capture_response_bodies'):return
        if len(self.tasks)>=16 or self.omitted:
            self.body_omitted+=1;return
        task=asyncio.create_task(self.body(request));self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def body(self,request):
        try:
            async with asyncio.timeout(3):
                response=await request.response()
                if response is None:return
                sizes=await request.sizes()
                length=sizes.get('responseBodySize',-1)
                mime=response.headers.get('content-type','').lower()
                if length<0 or length>self.body_limit or not any(part in mime for part in ('text/','json','javascript','xml')):
                    self.body_omitted+=1;return
                # responseBodySize is encoded transfer size. Avoid allocating a
                # potentially huge decoded compressed body just to truncate it.
                if response.headers.get('content-encoding','identity')!='identity':
                    self.body_omitted+=1;return
                body=await controlled(self.context,response.body())
                if len(body)>self.body_limit:
                    self.body_omitted+=1;return
                self.emit({'event_type':'response_body','url':self.scrub(public_url(response.url)),
                    'text':self.scrub(body.decode('utf-8',errors='replace')),'source_bytes':len(body),
                    'redacted':True,'encoding':'utf-8 with replacement'})
        except asyncio.CancelledError:raise
        except Exception:
            self.errors+=1

    async def tls_details(self,response):
        if not self.tls:return None
        if response is None:return {'status':'unavailable','reason':'no_navigation_response'}
        try:
            details=await response.security_details()
            from .tls import browser_certificates, save_certificate_evidence
            certificates=await browser_certificates(self.page,response,
                ignore_https_errors=bool(self.params.get('browser_options',{}).get('ignore_https_errors')),session=self.tls_session)
            certificates=await save_certificate_evidence(self.context,response.url,certificates)
            return {**certificates,'details':details,'server':await response.server_addr()}
        except Exception:return {'status':'unavailable','reason':'browser_security_details_unavailable'}

    async def finish(self,response=None):
        if self.final is not None:return self.final
        if self.adapter_console and not self.page.is_closed():
            for message in await self.adapter.retrieve_console_messages(self.page):
                if message.get('type')=='capture_omitted':
                    self.omitted+=int(message.get('text',0));continue
                self.emit({'event_type':'console','type':message.get('type','log'),
                    'text':self.scrub(message.get('text','')),'capture':'page_instrumentation'})
        for event,handler in self.listeners:self.page.remove_listener(event,handler)
        self.listeners.clear()
        if self.tasks:
            done,pending=await asyncio.wait(list(self.tasks),timeout=1)
            self.body_omitted+=len(pending)
            for task in pending:task.cancel()
            await asyncio.gather(*done,*pending,return_exceptions=True)
        tls=await self.tls_details(response)
        self.stopped=True
        incomplete=bool(self.omitted or self.errors or self.pending_requests or self.body_omitted)
        report={'schema_version':1,'events':self.events,'tls':tls,
            'coverage':{'complete':not incomplete,'scope':'page events between capture start and stop',
                        'events':len(self.events),'event_bytes':self.bytes,'omitted_events':self.omitted,
                        'omitted_bodies':self.body_omitted,'inflight_requests':len(self.pending_requests),'capture_errors':self.errors,
                        'redaction':'URL credentials/query/fragment; credential headers omitted; known selected-account values and common secret forms redacted',
                        'response_bodies_requested':bool(self.params.get('capture_response_bodies')),
                        'post_data_retained':False,'original_unredacted_log':False}}
        if tls and tls['status']=='unavailable':report['coverage']['complete']=False
        stem=hashlib.sha256((str(self.page.url)+str(time.time_ns())).encode()).hexdigest()[:24]
        path=self.context.paths.run(self.context.job_id)/(stem+'.browser.json')
        raw=json.dumps(report,ensure_ascii=False,indent=2).encode()
        await asyncio.to_thread(path.write_bytes,raw)
        self.final={'coverage':report['coverage'],'tls':tls,'artifact':str(path),
                    'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        return self.final

    async def close(self):
        if self.closed:return
        self.closed=True;self.stopped=True
        for event,handler in self.listeners:self.page.remove_listener(event,handler)
        self.listeners.clear()
        for task in list(self.tasks):task.cancel()
        if self.tasks:await asyncio.gather(*list(self.tasks),return_exceptions=True)
        if self.tls_session is not None:
            try:await self.tls_session.detach()
            except Exception:pass
            self.tls_session=None
