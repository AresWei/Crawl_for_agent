from __future__ import annotations

import asyncio
import copy
from contextlib import aclosing
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from ..core.browser_providers import browser_error_types
BrowserError = browser_error_types()

from ..core.jobs import BudgetExceeded, JobContext, JobControl
from ..core.records import Record
from ..core.sessions import budget_for, controlled, open_browser, open_http
from .cache import PageCache, utc_now
from .cache_fingerprint import compute_head_fingerprint
from .cache_validator import CacheValidator, CacheValidationResult
from .deep_crawling.support import head_fetcher
from .document import extract_document, normalize_url
from .frontier import Frontier
from .dispatch import DispatchPolicy, DispatchMemoryPressure
from .streaming import results_in_order
from .inputs import local_path, process_pdf, read_local
from .hooks import BrowserHooks, HookError, ScriptTimeout, execute_scripts, wait_for_javascript
from .http_encoding import decode_http_text
from .block_detection import assess_blocking


class LimitReached(Exception):
    pass


def classify_source(payload, params, url):
    if isinstance(payload.get('status_code'),int) and payload['status_code']>=400:
        payload['http_error_response']=True
    if params.get('detect_blocking'):
        supplied=url.startswith('file:') or url in params.get('_raw_inputs',{}) or params.get('raw_html') is not None
        kind=('local' if url.startswith('file:') else 'raw') if supplied else payload['render_mode']
        payload['blocking']=assess_blocking(payload.get('status_code'),payload['html'],enabled=True,
            source_kind=kind,downloaded=bool(payload.get('downloads')) and not payload['html'])
    payload['source_usable']=not (payload.get('blocking',{}).get('exclude_from_answers') or payload.get('http_error_response'))


class WebSession:
    def __init__(self, ctx: JobContext, *, hooks: BrowserHooks | None = None):
        self.ctx, self.params = ctx, ctx.params
        self.client = self.browser = self.context = None
        self.adapter = None
        self.budget = budget_for(ctx)
        self.max_requests = max(1, min(10000, int(self.params.get("max_requests", 200))))
        self.budget.maximum_requests = min(self.budget.maximum_requests, self.max_requests)
        self.max_html_bytes = max(1024, min(50_000_000, int(self.params.get("max_html_bytes", 5_000_000))))
        self.timeout = max(1, min(120, float(self.params.get("timeout_seconds", 30))))
        self.browser_launches = 0
        self._browser_lock = asyncio.Lock()
        self._http_lock = asyncio.Lock()
        self._domain_locks = {}
        self.pages, self.details = {}, {}
        self.external_page = None
        self.downloads = None
        self.browser_session = None
        self.snapshots = {}
        self.http_tls = {}
        if self.params.get('remove_consent_popups'):
            raise ValueError('remove_consent_popups source behavior accepts privacy choices; it is not integrated as automatic collection')
        for name in ('text_mode','light_mode','adjust_viewport_to_content','override_navigator','simulate_user','magic'):
            if type(self.params.get(name,False)) is not bool:
                raise ValueError(name+' must be a boolean')
        if type(self.params.get('ignore_body_visibility',True)) is not bool:
            raise ValueError('ignore_body_visibility must be boolean')
        for name, low, high in (('wait_for_timeout',1,120000),('body_visibility_timeout',1,120000),('screenshot_wait_for',0,10)):
            value = self.params.get(name)
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high):
                raise ValueError(name+' is outside supported finite bounds')
        if self.params.get('adjust_viewport_to_content') and not self.params.get('text_mode'):
            if self.params.get('cdp_url') or self.params.get('browser_type','chromium') != 'chromium':
                raise ValueError('Content viewport adjustment requires an owned Chromium page')
        if type(self.params.get('detect_blocking',False)) is not bool:
            raise ValueError('detect_blocking must be a boolean')
        if type(self.params.get('verify_ssl',True)) is not bool:
            raise ValueError('verify_ssl must be a boolean')
        if sum(self.params.get(key) is not None for key in ('http_json','http_body','http_form'))>1:
            raise ValueError('Choose one HTTP body, JSON or form payload')
        form=self.params.get('http_form')
        if form is not None:
            if not isinstance(form,dict) or any(not isinstance(key,str) for key in form):
                raise ValueError('HTTP form must be an object with string keys')
            for value in form.values():
                values=value if isinstance(value,list) else [value]
                if any(item is not None and type(item) not in (str,int,float,bool) for item in values):
                    raise ValueError('HTTP form values must be scalars or lists of scalars')
        if self.params.get('screenshot_mode','native') not in {'native','stitched'}:
            raise ValueError('screenshot_mode must be native or stitched')
        self.hooks = hooks if hooks is not None else BrowserHooks(self.params.get('hooks'))
        self._browser_hook_events = []
        self._browser_created_callback = self._on_browser_created
        if self.params.get('js_execution_order', 'before_wait') not in {'before_wait', 'upstream'}:
            raise ValueError('js_execution_order must be before_wait or upstream')
        if not isinstance(self.params.get('session_steps', []), list) or len(self.params.get('session_steps', [])) > 100:
            raise ValueError('session_steps must be a list of at most 100 steps')
        from ..core.browser_profiles import SessionPageRegistry
        self.session_pages = SessionPageRegistry(ttl_seconds=self.params.get('session_ttl_seconds',1800))
        ctx.resources.push_async_callback(self.session_pages.close)

    @property
    def requests(self):
        return self.budget.requests

    @property
    def blocked_requests(self):
        return self.budget.blocked

    async def http_bytes(self, url, redirects=0):
        await self.ensure_http()
        method = self.params.get("http_method", "GET").upper()
        kwargs = {}
        if self.params.get("http_json") is not None:
            kwargs["json"] = self.params["http_json"]
        elif self.params.get("http_body") is not None:
            kwargs["content"] = self.params["http_body"]
        elif self.params.get('http_form') is not None:
            kwargs['data'] = self.params['http_form']
        request=self.client.build_request(method,url,**kwargs)
        maximum=min(20,int(self.params.get('max_redirects',10)))
        while redirects<=maximum:
            if self.requests>=self.max_requests:
                self.budget.reason = self.budget.reason or 'request_budget'
                raise BudgetExceeded(self.budget.reason)
            domain=request.url.host
            lock=self._domain_locks.setdefault(domain,asyncio.Lock())
            async with lock:result=await self._http_once(request)
            if not isinstance(result,httpx.Request):return result
            request=result;redirects+=1
        raise httpx.TooManyRedirects('Redirect limit exceeded')

    async def _http_once(self, request):
        # HTTPX constructs next_request with correct 301/302/303 versus307/308
        # method/body semantics and scoped cookies. Rebuilding from initial
        # params would both resend POST and leak Authorization across origins.
        response=await self.client.send(request,stream=True,follow_redirects=False)
        async with aclosing(response):
            if response.is_redirect and response.headers.get("location") and self.params.get("follow_redirects", True):
                redirected=response.next_request
                if redirected is None:raise ValueError('Redirect target unavailable')
                normalize_url(str(redirected.url))
                if (request.url.scheme,request.url.host,request.url.port)!=(redirected.url.scheme,redirected.url.host,redirected.url.port):
                    for name in ('authorization','proxy-authorization','x-api-key'):
                        redirected.headers.pop(name,None)
                return redirected
            if not self.params.get('detect_blocking'):
                response.raise_for_status()
            if self.params.get('fetch_ssl_certificate'):
                from .tls import http_certificate, save_certificate_evidence
                self.http_tls[str(response.url)] = await save_certificate_evidence(self.ctx,str(response.url),http_certificate(response))
            data = bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > self.max_html_bytes:
                    raise LimitReached("html_byte_budget")
            # Credential-bearing response headers must not enter evidence records.
            headers = {k: v for k, v in response.headers.items() if k.lower() in {
                "content-type", "content-length", "etag", "last-modified", "cache-control", "date", "location"}}
            captured = bytes(data), str(response.url), response.status_code, response.encoding or "utf-8", headers
            # Keep the bounded error body available after the dispatcher's last
            # attempt, without disabling its Retry-After/backoff behavior.
            if self.params.get('detect_blocking') and response.is_error:
                response.extensions['research_captured_response'] = captured
                response.raise_for_status()
            return captured

    async def ensure_http(self):
        async with self._http_lock:
            if self.client is None:
                self.client = await open_http(self.ctx, platform="web", account=self.params.get("account"), timeout=self.timeout,
                    verify=self.params.get('verify_ssl',True),
                    headers={"User-Agent": "ResearchCrawler/0.1 (local research)", **self.params.get("headers", {})})
        return self.client

    async def http(self, url, redirects=0):
        data, final_url, status, encoding, headers = await self.http_bytes(url, redirects)
        decoded = await asyncio.to_thread(decode_http_text,data,headers,encoding=self.params.get('encoding'))
        self.details[url] = {"response_headers": headers, "source_bytes": len(data),'text_decoding':decoded.evidence(),
                             'http_tls_verification':self.params.get('verify_ssl',True)}
        return decoded.text, final_url, status

    async def peek_head(self, url):
        document, _, _ = await self.http(url)
        return document

    @property
    def launched_browsers(self):
        if self.browser_session is None or self.params.get('cdp_url'):
            return ()
        if hasattr(self.browser_session, 'session_for'):
            return tuple(g['session'].browser for g in self.browser_session.generations)
        return (self.browser,)

    async def ensure_browser(self):
        async with self._browser_lock:
            if self.context is not None:
                return
            options = self.params.get("browser_options", {})
            session = await open_browser(self.ctx, platform="web", account=self.params.get("account"),
                headless=self.params.get("headless", True), viewport=self.params.get("viewport"),
                init_scripts=self.params.get("init_scripts", []), headers=self.params.get("headers"),
                context_options=options, launch_args=self.params.get("launch_args", []),
                accept_downloads=self.params.get('accept_downloads',False),
                on_browser_created=self._browser_created_callback if self.hooks.active else None)
            self.browser, self.context = session.browser, session.context
            self.adapter = session.adapter
            self.browser_error = self.adapter.get_imports()[1]
            self.browser_session = session
            self.downloads = session.downloads
            self.browser_launches += 0 if self.params.get('cdp_url') else 1
            if session.page_owned:
                await session.page.close()
            else:
                self.external_page = session.page

    async def _on_browser_created(self, browser, *, context):
        if 'on_browser_created' in self.hooks.native:
            await self.hooks.execute('on_browser_created', self.ctx, browser, context=context)
            self._browser_hook_events.append('on_browser_created')

    async def hook(self, name, page, results, **kwargs):
        if name not in self.hooks.native and name not in self.hooks.javascript:
            return None
        hook_context = self.browser_session.session_for(page).context if hasattr(self.browser_session,'session_for') else self.context
        if name == 'before_return_html':
            value = await self.hooks.execute(name, self.ctx, page=page, context=hook_context, config=self.params, adapter=self.adapter, **kwargs)
        else:
            value = await self.hooks.execute(name, self.ctx, page, context=hook_context, config=self.params, adapter=self.adapter, **kwargs)
        results.append(name)
        return value

    async def browse(self, url, document=None):
        if self.params.get('c4a_script') and self.params.get('cdp_url'):
            raise ValueError('C4A execution currently requires an owned browser page')
        await self.ensure_browser()
        session_id = self.params.get("session_id")
        retained = (await self.session_pages.get(session_id) if session_id else None) or self.external_page
        if self.params.get("js_only") and retained is None:
            raise ValueError("js_only requires an existing session in this job")
        page = retained or await self.context.new_page()
        browser_session = self.browser_session.session_for(page) if hasattr(self.browser_session, "session_for") else self.browser_session
        downloads = browser_session.downloads
        if session_id:
            self.pages[session_id] = page
            if page != self.external_page:
                await self.session_pages.put(session_id,page)
        page.set_default_timeout(self.timeout * 1000)
        hook_results, response = list(self._browser_hook_events), None
        self._browser_hook_events.clear()
        script_results = []
        script_interrupted = False
        async def run_scripts(value, phase):
            result = await execute_scripts(page, value, self.ctx, adapter=self.adapter)
            script_results.append({'phase': phase, **result['summary']})
            return {'success': result['summary']['success'], 'results': result['values']}
        download_navigation = False
        interaction_receipt = None
        observation = None
        content_viewport = None
        viewport_receipt = None
        visibility_receipt = None
        if any(self.params.get(key) for key in ('capture_network_requests','capture_console_messages','capture_response_bodies','fetch_ssl_certificate')):
            from .browser_evidence import BrowserEvidence
            observation = await BrowserEvidence(self.ctx,page,adapter=browser_session.adapter).start()
        try:
            # Frozen upstream dispatches this event for each crawl, including reused pages.
            await self.hook("on_page_context_created", page, hook_results)
            if not self.params.get("js_only") and (page != self.external_page or self.params.get('cdp_navigate',False)):
                await self.hook("before_goto", page, hook_results, url=url)
                if document is not None:
                    await page.set_content(document, wait_until="domcontentloaded")
                else:
                    download_cursor = len(downloads.snapshot()) if downloads else 0
                    try:
                        response = await page.goto(url, wait_until=self.params.get("wait_until", "domcontentloaded"))
                    except self.browser_error as error:
                        expected_download=any(marker in str(error) for marker in ('net::ERR_ABORTED','Download is starting'))
                        if not downloads or not expected_download or not await downloads.wait_started(page,url,timeout=1,after=download_cursor):
                            raise
                        download_navigation = True
                    if response is not None and response.status >= 400:
                        raise ValueError("http_status_" + str(response.status))
                await self.hook("after_goto", page, hook_results, url=url, response=response)
            if 'ignore_body_visibility' in self.params or self.params.get('body_visibility_timeout') is not None:
                visibility_timeout = self.params.get('body_visibility_timeout',30000) / 1000
                try:
                    await wait_for_javascript(page, "() => {const e=document.body;if(!e)return false;const s=getComputedStyle(e);return s.display!=='none' && s.visibility!=='hidden' && s.opacity!=='0';}",
                        self.ctx, adapter=self.adapter, timeout=visibility_timeout)
                    visibility_receipt = {'status':'visible','timeout_seconds':visibility_timeout}
                except TimeoutError:
                    if not self.params.get('ignore_body_visibility',True):
                        raise ValueError('Body element is hidden or unavailable') from None
                    visibility_receipt = {'status':'timeout','continued':True,'timeout_seconds':visibility_timeout}
            c4a_result = None
            if self.params.get('c4a_script'):
                from .c4a.runtime import execute_c4a
                c4a_result = await execute_c4a(page, self.params, self.ctx)
                if not c4a_result['success']:
                    self.details[url] = {'c4a_result': c4a_result, 'hooks_executed': hook_results,
                        'capture_interrupted': page.is_closed()}
                    captured = '' if page.is_closed() else await page.content()
                    artifacts = {}
                    if observation:
                        diagnostic = await observation.finish(response)
                        self.details[url]['browser_evidence'] = diagnostic
                        artifacts['browser_evidence'] = diagnostic['artifact']
                    if downloads:
                        receipts = await downloads.settle(page)
                        self.details[url]['downloads'] = receipts
                        artifacts.update({'download_'+item['guid']:item['path'] for item in receipts if item['status']=='complete'})
                    return captured, page.url if not page.is_closed() else url, c4a_result.get('navigation_status',response.status if response else None), artifacts
            if self.params.get('js_code_before_wait'):
                await run_scripts(self.params['js_code_before_wait'], 'before_wait')
            upstream_order = self.params.get('js_execution_order', 'before_wait') == 'upstream'
            if self.params.get('js_code') and not upstream_order:
                execution_result = await run_scripts(self.params['js_code'], 'legacy_before_wait')
                await self.hook('on_execution_started', page, hook_results)
                await self.hook('on_execution_ended', page, hook_results, result=execution_result)
            if self.params.get('simulate_user') or self.params.get('magic'):
                from ..core.browser_behavior import simulate_interaction
                interaction_receipt = await simulate_interaction(page, self.ctx)
            wait = self.params.get("wait_for")
            if wait:
                wait_timeout = self.params.get('wait_for_timeout')
                wait_seconds = self.timeout if wait_timeout is None else wait_timeout / 1000
                if wait.startswith("js:"):
                    await wait_for_javascript(page, wait[3:], self.ctx, adapter=self.adapter, timeout=wait_seconds)
                else:
                    await page.wait_for_selector(wait.removeprefix("css:"), state="attached", timeout=wait_seconds * 1000)
            image_wait = None
            if (self.params.get("wait_for_images") or self.params.get('adjust_viewport_to_content')) and not self.params.get('text_mode'):
                from .content_viewport import wait_content_images
                image_wait = await wait_content_images(page, self.ctx, self.adapter,
                    timeout=self.timeout if self.params.get('wait_for_images') else 1,
                    strict=bool(self.params.get('wait_for_images')))
            if self.params.get('adjust_viewport_to_content'):
                if self.params.get('text_mode'):
                    viewport_receipt = {'status':'skipped','reason':'text_mode'}
                else:
                    from .content_viewport import ContentViewport
                    content_viewport = ContentViewport(page,self.adapter)
                    viewport_receipt = await content_viewport.apply()
            await page.wait_for_timeout(max(0, min(5000, int(self.params.get("settle_ms", 250)))))
            steps = max(0, min(200, int(self.params.get("scroll_steps", 0))))
            if self.params.get("scan_full_page"):
                steps = max(steps, min(100, int(self.params.get("max_scroll_steps", 30))))
            for _ in range(steps):
                await self.ctx.check_control()
                previous = await self.adapter.evaluate(page, "[scrollY,document.documentElement.scrollHeight]")
                selector = self.params.get("scroll_container")
                if selector:
                    await page.locator(selector).evaluate("e => e.scrollBy(0,e.clientHeight)")
                else:
                    await self.adapter.evaluate(page, "window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(max(0, min(2000, int(self.params.get("scroll_delay_ms", 150)))))
                current = await self.adapter.evaluate(page, "[scrollY,document.documentElement.scrollHeight]")
                if self.params.get("scan_full_page") and not selector and current == previous:
                    break
            for action in self.params.get("session_steps", []):
                await self.ctx.check_control()
                if action.get("js_code"):
                    await run_scripts(action['js_code'], 'session_step')
                if action.get("wait_for"):
                    value = action["wait_for"]
                    if value.startswith('js:'):
                        await wait_for_javascript(page, value[3:], self.ctx, adapter=self.adapter, timeout=self.timeout)
                    else:
                        await page.wait_for_selector(value.removeprefix('css:'))
            await self.hook("before_retrieve_html", page, hook_results)
            if self.params.get('js_code') and upstream_order:
                execution_result = await run_scripts(self.params['js_code'], 'after_wait')
                # Preserve the frozen upstream's actual (post-execution) order.
                await self.hook('on_execution_started', page, hook_results)
                await self.hook('on_execution_ended', page, hook_results, result=execution_result)
            dom_details = {}
            if any(self.params.get(key) for key in ('process_iframes','flatten_shadow_dom','remove_overlay_elements','virtual_scroll_config')):
                from .dom_processing import process_dom
                transformed = await process_dom(page,self.params,check_control=self.ctx.check_control)
                captured = transformed['html']
                dom_details = {'raw_html':transformed['raw_html'], 'dom_coverage':transformed['coverage'],
                    'dom_transformations':transformed['transformations'], 'artifact_dom_state':'after_requested_transforms'}
            else:
                captured = await page.content()
            if viewport_receipt is not None:
                dom_details['content_viewport'] = viewport_receipt
            try:
                hook_value = await self.hook('before_return_html', page, hook_results, html=captured)
                if 'before_return_html' in self.hooks.transforms:
                    if not isinstance(hook_value, str):
                        raise HookError('browser_hook_invalid_html:before_return_html')
                    captured = hook_value
                    dom_details['html_hook_transformed'] = True
            except HookError as error:
                dom_details['hook_failure'] = str(error)
                # Keep already captured source evidence on a late hook failure.
                if 'timeout' in str(error):
                    script_interrupted = True
            if len(captured.encode("utf-8")) > self.max_html_bytes:
                raise LimitReached("html_byte_budget")
            artifacts = {}
            # Keep an already acquired DOM even if later resource accounting or
            # controlled() raises before collect() can return it to the caller.
            self.snapshots[url]={'html':captured,'final_url':url if document is not None else page.url,
                'status_code':response.status if response else None,'artifacts':artifacts,'source_fetched_at':utc_now()}
            stem = hashlib.sha256(url.encode()).hexdigest()[:24]
            for kind in (() if script_interrupted else ("screenshot", "pdf", "mhtml")):
                if not self.params.get(kind):
                    continue
                output = self.ctx.paths.run(self.ctx.job_id) / (stem + {"screenshot": ".png", "pdf": ".pdf", "mhtml": ".mhtml"}[kind])
                temporary = output.with_suffix(output.suffix + ".tmp")
                if kind == "screenshot":
                    if self.params.get('screenshot_wait_for'):
                        await controlled(self.ctx, asyncio.sleep(self.params['screenshot_wait_for']))
                    if self.params.get('screenshot_mode')=='stitched':
                        from .screenshots import capture_stitched
                        artifacts['screenshot_receipt']=str(output.with_suffix(output.suffix+'.receipt.json'))
                        def screenshot_finished(receipt):
                            dom_details['screenshot']=receipt
                            if receipt.get('path'):artifacts['screenshot']=receipt['path']
                            if not receipt.get('receipt_path'):artifacts.pop('screenshot_receipt',None)
                        await capture_stitched(page,output,self.params.get('screenshot_stitch'),context=self.ctx,adapter=self.adapter,on_receipt=screenshot_finished)
                        continue
                    await page.screenshot(path=str(temporary), type="png", full_page=self.params.get("screenshot_full_page", True))
                elif kind == "pdf":
                    await page.pdf(path=str(temporary), print_background=True, **self.params.get("pdf_options", {}))
                else:
                    cdp = await self.context.new_cdp_session(page)
                    try:
                        snapshot = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
                        await asyncio.to_thread(temporary.write_text, snapshot["data"], encoding="utf-8")
                    finally:
                        await cdp.detach()
                temporary.replace(output)
                artifacts[kind] = str(output)
            if downloads:
                receipts = await downloads.settle(page)
                dom_details['downloads'] = receipts
                artifacts.update({'download_'+item['guid']:item['path'] for item in receipts if item['status']=='complete'})
            if observation:
                diagnostic = await observation.finish(response)
                artifacts['browser_evidence'] = diagnostic['artifact']
                dom_details['browser_evidence'] = diagnostic
            if browser_session.network_bridge is not None:
                await controlled(self.ctx,browser_session.network_bridge.settle())
                await self.ctx.check_control()
            from ..core.browser_behavior import navigator_receipt
            self.details[url] = {"hooks_executed": hook_results, "session_id": session_id,
                                 'navigator_override': navigator_receipt(self.params),
                                 'interaction': interaction_receipt,
                                 'browser_modes': browser_session.browser_modes,
                                 'image_wait': image_wait,
                                 'body_visibility': visibility_receipt,
                                 'screenshot_wait_seconds': self.params.get('screenshot_wait_for',0) if self.params.get('screenshot') else None,
                                 **({'browser_recycle':dict(self.browser_session.receipt)} if hasattr(self.browser_session,'session_for') else {}),
                                 'javascript_execution': {'success': all(item['success'] for item in script_results), 'phases': script_results},
                                 'js_execution_order': self.params.get('js_execution_order', 'before_wait'),
                                 "session_steps": len(self.params.get("session_steps", [])),
                                 "download_navigation":download_navigation,
                                 **dom_details, **({'c4a_result':c4a_result} if c4a_result is not None else {}),
                                 "external_browser": bool(self.params.get('cdp_url')),
                                 "network_observation_scope": 'since_attachment' if self.external_page else 'owned_navigation'}
            final_url = page.url if c4a_result and 'navigation_status' in c4a_result else url if document is not None or page.url=='about:blank' else page.url
            final_status = c4a_result.get('navigation_status',response.status if response else None) if c4a_result else response.status if response else None
            return captured, final_url, final_status, artifacts
        except ScriptTimeout as error:
            script_interrupted = True
            self.details[url] = {'hooks_executed': hook_results, 'javascript_execution': error.summary,
                                 'capture_interrupted': True}
            return '', url, response.status if response else None, {}
        finally:
            try:
                if content_viewport:
                    await content_viewport.restore()
            finally:
                if observation:
                    await observation.close()
                if session_id and page != self.external_page:
                    self.session_pages.touch(session_id)
                if (not session_id or script_interrupted) and page != self.external_page:
                    await page.close()
                if hasattr(self.browser_session, "drain"):
                    await self.browser_session.drain()

    async def pdf_document(self, data, url, path=None):
        if path is None:
            path = self.ctx.paths.run(self.ctx.job_id) / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".input.pdf")
            await asyncio.to_thread(path.write_bytes, data)
        images = self.ctx.paths.run(self.ctx.job_id) / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".images")
        document, metadata = await controlled(self.ctx, asyncio.to_thread(process_pdf, path, self.params, images))
        self.details[url] = {**self.details.get(url, {}), "pdf_document": metadata,
                             "artifacts": {**self.details.get(url, {}).get("artifacts", {}), **metadata.get("image_artifacts", {})},
                             "source_bytes": len(data), "source_sha256": hashlib.sha256(data).hexdigest(),
                             "pdf_truncated": len(metadata["pages"]) < metadata["metadata"]["pages"]}
        return document

    async def collect(self, url):
        from ..core.browser_providers import validate_provider_options
        validate_provider_options(self.params)
        render = self.params.get("render", "auto")
        if render not in {"auto", "browser", "http"}:
            raise ValueError("render must be auto, browser or http")
        raw = self.params.get("_raw_inputs", {}).get(url, self.params.get("raw_html"))
        if url.startswith("file:"):
            data, path = await asyncio.to_thread(read_local, url, self.max_html_bytes)
            if data.startswith(b"%PDF"):
                return await self.pdf_document(data, url, path), url, None, {}, "pdf"
            decoded = await asyncio.to_thread(decode_http_text,data,encoding=self.params.get('encoding'))
            raw = decoded.text
            self.details[url] = {"source_bytes": len(data), "source_sha256": hashlib.sha256(data).hexdigest(),
                                 'text_decoding':decoded.evidence()}
        if raw is not None and len(raw.encode("utf-8")) > self.max_html_bytes:
            raise LimitReached("html_byte_budget")
        forced = 'ignore_body_visibility' in self.params or self.hooks.active or any(self.params.get(k) for k in ("body_visibility_timeout", "avoid_css", "avoid_ads", "memory_saving_mode", "override_navigator", "simulate_user", "magic", "text_mode", "light_mode", "max_pages_before_recycle", "adjust_viewport_to_content", "screenshot", "pdf", "mhtml", "wait_for", "js_code", "js_code_before_wait", "scroll_steps", "session_steps", "scan_full_page", "hooks", "cdp_url", "browser_profile", "browser_type", "browser_provider", "process_iframes", "flatten_shadow_dom", "remove_overlay_elements", "virtual_scroll_config", "c4a_script", "capture_network_requests", "capture_console_messages", "capture_response_bodies", "accept_downloads"))
        if render == "browser" or forced:
            document, final_url, status, artifacts = await self.browse(url, raw)
            return document, final_url, status, artifacts, "browser"
        if raw is not None:
            return raw, url, None, {}, "local" if url.startswith("file:") else "raw"
        data, final_url, status, encoding, headers = await self.http_bytes(url)
        self.details[url] = {"response_headers": headers, "source_bytes": len(data),
                             'source_sha256':hashlib.sha256(data).hexdigest(),
                             'http_tls_verification':self.params.get('verify_ssl',True)}
        tls=self.http_tls.get(final_url)
        if tls is not None:
            self.details[url]['tls']=tls
        if data.startswith(b"%PDF") or "application/pdf" in headers.get("content-type", ""):
            return await self.pdf_document(data, url), final_url, status, {}, "pdf"
        decoded = await asyncio.to_thread(decode_http_text,data,headers,encoding=self.params.get('encoding'))
        document = decoded.text
        self.details[url]['text_decoding'] = decoded.evidence()
        if render == "auto" and BeautifulSoup(document, "lxml").find("script"):
            document, final_url, status, artifacts = await self.browse(url)
            return document, final_url, status, artifacts, "browser"
        tls_artifacts={'tls_'+key:entry['path'] for key,entry in (tls or {}).get('exports',{}).items()}
        return document, final_url, status, tls_artifacts, "http"


async def collect_web(ctx: JobContext) -> dict[str, Any]:
    from .configs import normalize_params
    from .crawlers import prepare_crawler_params
    params = prepare_crawler_params(normalize_params(ctx.params))
    raw_urls = params.get("urls", [])
    raw_urls = [raw_urls] if isinstance(raw_urls, str) else raw_urls
    if not isinstance(raw_urls, list) or len(raw_urls) > 1000:
        raise ValueError("urls must contain at most 1000 URLs")
    urls = []
    for value in raw_urls:
        if value.startswith("file:"):
            urls.append(local_path(value).as_uri())
        elif value.startswith("raw:"):
            raw = value[6:] if value.startswith("raw://") else value[4:]
            key = "raw://" + hashlib.sha256(raw.encode()).hexdigest()
            params = {**params, "_raw_inputs": {**params.get("_raw_inputs", {}), key: raw}}
            urls.append(key)
        else:
            urls.append(normalize_url(value))
    for value in params.get("local_files", []):
        urls.append(Path(value).resolve(strict=True).as_uri())
    urls = list(dict.fromkeys(urls))
    if not urls and params.get("raw_html") is not None:
        urls = ["raw://" + hashlib.sha256(params["raw_html"].encode()).hexdigest()]
    if not urls:
        raise ValueError("Supply urls, local_files or raw_html")
    if params.get('adaptive') is not None:
        if params.get('strategy',params.get('deep_strategy','best_first')) not in {'best_first','adaptive'}:
            raise ValueError('adaptive exploration uses best_first ordering; omit strategy or select best_first')
        params = {**params, 'strategy':'best_first'}
    if params is not ctx.params:
        ctx.params = params
    max_pages = max(1, min(500, int(params.get("max_pages", 8))))
    concurrency = max(1, min(8, int(params.get("concurrency", 2))))
    stream = params.get('stream', False)
    if not isinstance(stream, bool):
        raise ValueError('stream must be a boolean')
    if params.get("session_id") or params.get('target_id'):
        concurrency = 1
    if params.get('target_id') and not params.get('cdp_navigate',False):
        if len(urls)!=1 or params.get('max_depth',0):
            raise ValueError('An existing target snapshot accepts one source and no link traversal; use cdp_navigate for explicit sequential navigation')
    maximum_seconds = max(1, min(3600, float(params.get("max_seconds", 120))))
    checkpoint = await ctx.checkpoint("web")
    revision = checkpoint["revision"] if checkpoint else 0
    state = checkpoint["cursor"] if checkpoint else {"pending": [{"url": u, "depth": 0} for u in urls], "visited": [], "failed": []}
    saved_adaptive=state.get('adaptive',{})
    saved_analysis_limits=saved_adaptive.get('limits',[]) or saved_adaptive.get('embedding',{}).get('limits',[])
    if checkpoint and checkpoint["exhausted"] and not saved_analysis_limits:
        return {"status": "complete", "pages": len(state["visited"]), "resumed": True}
    if checkpoint and not state["pending"] and state["failed"]:
        state["pending"], state["failed"] = state["failed"], []
    frontier, session, cache = Frontier(params, urls), WebSession(ctx), PageCache(ctx)
    from .url_configs import URLRunConfigs,ParamsContext
    url_configs=URLRunConfigs(params) if 'run_configs' in params else None
    from .user_agents import resolve_user_agent, _configuration
    from ..core.proxy_policy import ProxyPolicy
    from ..core.accounts import ProxyStore
    capture_scopes={};selected_configs={};resolved_scopes={};proxy_policies={};network_identities={}
    identity_lock=asyncio.Lock()
    state.setdefault('user_agents',{})
    state.setdefault('proxy_policies',{})
    async def capture_scope(url):
        # One lock covers first selection, including a shared online catalogue
        # request; parallel capture/retry cannot create conflicting receipts.
        async with identity_lock:
            if url in resolved_scopes:return resolved_scopes[url]
            options,selection=url_configs.resolve(url) if url_configs else (copy.deepcopy(params),None)
            if selection is not None:
                overrides=url_configs.entries[selection['index']][1]
                if overrides.get('browser_options',{}).get('user_agent') is not None:
                    # An explicit URL-level identity overrides the enclosing
                    # default generator; never mutate the original job params.
                    options.pop('user_agent',None)
                    options['headers']={k:v for k,v in options.get('headers',{}).items() if k.lower()!='user-agent'}
                    if 'user_agent_mode' not in overrides:options['user_agent_mode']='explicit'
                    if 'user_agent_generator_config' not in overrides:options.pop('user_agent_generator_config',None)
                elif overrides.get('user_agent_mode') in {'random','synthetic','online'}:
                    options.pop('user_agent',None)
                    options['browser_options']={k:v for k,v in options.get('browser_options',{}).items() if k!='user_agent'}
                    options['headers']={k:v for k,v in options.get('headers',{}).items() if k.lower()!='user-agent'}
            identity={}
            if options.get('proxy_policy') is not None:
                policy_key=hashlib.sha256(json.dumps({key:options.get(key) for key in ('proxy_policy','account','session_id','browser_profile')},sort_keys=True).encode()).hexdigest()
                if policy_key not in proxy_policies:
                    proxy_policies[policy_key]=ProxyPolicy(options,state['proxy_policies'].get(policy_key),
                        available_names=[entry['name'] for entry in ProxyStore(ctx.paths).list()])
                policy=proxy_policies[policy_key]
                options['proxy_id']=policy.select(url,source_id=url,session_id=options.get('session_id'),account=options.get('account'))
                state['proxy_policies'][policy_key]=policy.dump()
                identity['proxy']={'alias':options['proxy_id'],'policy_sha256':policy.identity,'response_triggered_rotation':False}
            elif options.get('proxy_id') is not None:
                identity['proxy']={'alias':options['proxy_id'],'response_triggered_rotation':False}
            from ..core.browser_behavior import magic_user_agent
            options=magic_user_agent(options)
            ua_config=_configuration(options)
            ua_key=hashlib.sha256(json.dumps(ua_config,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            snapshot=state['user_agents'].get(ua_key)
            if snapshot is None and state.get('user_agent',{}).get('config_sha256')==ua_key:
                snapshot=state['user_agent']
            client=None
            if ua_config['mode']=='online' and snapshot is None:
                client=await open_http(ParamsContext(ctx,options),platform='web',account=options.get('account'),timeout=5)
            receipt=await resolve_user_agent(options,snapshot,client=client)
            if receipt is not None:
                state['user_agents'][ua_key]=receipt
                identity['user_agent']={key:copy.deepcopy(receipt[key]) for key in ('source','config_sha256','choice_sha256','limitations')}
                headers={k:v for k,v in options.get('headers',{}).items() if k.lower()!='user-agent'}
                for key,value in receipt['headers'].items():
                    previous=[v for k,v in headers.items() if k.lower()==key.lower()]
                    if previous and any(v!=value for v in previous):raise ValueError('Conflicting generated '+key+' header')
                    headers={k:v for k,v in headers.items() if k.lower()!=key.lower()}
                    headers[key]=value
                options['headers']=headers
                options['browser_options']={**options.get('browser_options',{}),'user_agent':receipt['user_agent']}
            key=hashlib.sha256(json.dumps(options,sort_keys=True,default=str).encode()).hexdigest()
            if key not in capture_scopes:
                scoped=ParamsContext(ctx,options)
                web_session=WebSession(scoped)
                web_session.snapshots=session.snapshots
                web_session.details=session.details
                capture_scopes[key]=(scoped,options,web_session,PageCache(scoped))
            if selection is not None:selected_configs[url]=selection
            network_identities[url]=identity
            resolved_scopes[url]=capture_scopes[key]
            return resolved_scopes[url]
    adaptive = None
    if params.get('adaptive') is not None:
        from .adaptive import AdaptiveController
        adaptive = AdaptiveController(params['adaptive'], state.get('adaptive'),context=ctx)
    def adaptive_limits():
        if adaptive is None:return []
        return adaptive.limits or (adaptive.embedding.limits if adaptive.embedding else [])
    dispatch = DispatchPolicy(params,concurrency=concurrency,context=ctx)
    dispatch.restore_state(state.get('dispatch_state'))
    state["strategy"] = frontier.kind
    # Seed persistence before acquisition: a cancellation/crash cannot lose initial URLs.
    if checkpoint is None:
        checkpoint = await ctx.commit_page("web", [], next_cursor=state, exhausted=False, expected_revision=revision)
        revision = checkpoint["revision"]
    started, attempts, content_bytes, hits = time.monotonic(), 0, 0, 0
    errors, reason = [], state.get("coverage_limit")
    async def scoped_head(url):
        return await (await capture_scope(url))[2].peek_head(url)
    token = head_fetcher.set(scoped_head)
    acquired_sources = {}
    fair_hosts=state.setdefault('dispatch_fairness',{})
    def admitted(items):
        sequence=max(fair_hosts.values(),default=0)
        for item in items:
            sequence+=1
            fair_hosts[urlsplit(item['url']).hostname or urlsplit(item['url']).scheme]=sequence

    async def capture_source(item,scope):
        ctx,params,session,cache=scope
        url = item["url"]
        cached, cache_status = await asyncio.to_thread(cache.read, url)
        if cached is not None:
            if params.get("cache_validate") and url.startswith(("http:", "https:")):
                headers = cached.get("response_headers", {})
                validation = await CacheValidator(await session.ensure_http()).validate(url,
                    stored_etag=headers.get("etag"), stored_last_modified=headers.get("last-modified"),
                    stored_head_fingerprint=compute_head_fingerprint(cached["html"]))
                cached["cache"].update(validation=validation.status.value, validation_reason=validation.reason,
                    validation_scope="head_signals" if "fingerprint" in validation.reason else "conditional_http_response")
                if validation.status == CacheValidationResult.FRESH:
                    return cached, True
                cache_status = "validation_" + validation.status.value
            else:
                return cached, True
        remaining = maximum_seconds - (time.monotonic() - started)
        async with asyncio.timeout(max(.001, remaining)):
            browser_actions='ignore_body_visibility' in params or any(params.get(key) for key in ('body_visibility_timeout','avoid_css','avoid_ads','memory_saving_mode','override_navigator','simulate_user','magic','text_mode','light_mode','max_pages_before_recycle','adjust_viewport_to_content','browser_profile','browser_type','browser_provider','cdp_url','screenshot','pdf','mhtml','wait_for','js_code','js_code_before_wait','scroll_steps','session_steps','scan_full_page','hooks','process_iframes','flatten_shadow_dom','remove_overlay_elements','virtual_scroll_config','c4a_script','capture_network_requests','capture_console_messages','capture_response_bodies','accept_downloads'))
            try:
                document, final_url, status, artifacts, mode = await controlled(ctx, dispatch.run(item,
                    lambda:session.collect(url),method=params.get('http_method','GET'),
                    retry_allowed=params.get('render')=='http' and not browser_actions and url.startswith(('http:','https:'))))
            except httpx.HTTPStatusError as error:
                captured=error.response.extensions.get('research_captured_response')
                if not params.get('detect_blocking') or captured is None:raise
                data,final_url,status,_,headers=captured
                decoded=await asyncio.to_thread(decode_http_text,data,headers,encoding=params.get('encoding'))
                document,artifacts,mode=decoded.text,{},'http'
                session.details[url]={'response_headers':headers,'source_bytes':len(data),
                    'source_sha256':hashlib.sha256(data).hexdigest(),'text_decoding':decoded.evidence(),
                    'http_tls_verification':params.get('verify_ssl',True),'http_error_response':True}
            payload = await controlled(ctx, asyncio.to_thread(extract_document, document, final_url, params))
        payload.update({"requested_url": url, "final_url": final_url, "status_code": status,
                        "artifacts": artifacts, "render_mode": mode, "source_fetched_at": utc_now(),
                        "cache": {"status": cache_status}, **session.details.get(url, {})})
        classify_source(payload,params,url)
        if session.browser_session is not None and mode=='browser':
            browser_session=session.browser_session
            payload['browser_runtime']={'engine':browser_session.engine,'provider':browser_session.provider,
                'network_accounting':browser_session.network_bridge.evidence() if browser_session.network_bridge else
                {'mode':'Chromium CDP encoded-data events','budget_enforcement':'observed_stop_with_inflight_overshoot'}}
        if payload['source_usable'] and not payload.get("pdf_truncated") and not session.blocked_requests and payload.get('dom_coverage',{}).get('complete',True) and not payload.get('hook_failure') and payload.get('javascript_execution',{}).get('success',True):
            await asyncio.to_thread(cache.write, url, payload)
        return payload, False

    async def capture(item):
        scope=await capture_scope(item['url'])
        ctx,params,session,cache=scope
        payload, hit = await capture_source(item,scope)
        if network_identities.get(item['url']):payload['network_identity']=copy.deepcopy(network_identities[item['url']])
        if hit:
            classify_source(payload,params,item['url'])
        if item['url'] in selected_configs:payload['run_configuration']=selected_configs[item['url']]
        acquired_sources[item['url']] = (item, payload, hit)
        if payload.get('source_usable') is False:
            return payload,hit
        if params.get('crawler') is not None:
            from .crawlers import enrich_crawler
            remaining=maximum_seconds-(time.monotonic()-started)
            try:
                async with asyncio.timeout(max(.001,remaining)):
                    payload['crawler']=await controlled(ctx,enrich_crawler(payload,params['crawler'],context=ctx,identity_scope=cache.partition))
            except TimeoutError:
                payload['crawler']={'status':'partial','records':[],'limits':['job_time_budget']}
            except (ValueError,TypeError,ImportError) as error:
                payload['crawler']={'status':'partial','records':[],'limits':['crawler_processing_error'],'error_type':type(error).__name__}
        for parameter,key in [('schema_inference','schema_inference'),('llm_tables','llm_tables')]:
            if params.get(parameter) is None:continue
            remaining=maximum_seconds-(time.monotonic()-started)
            try:
                async with asyncio.timeout(max(.001,remaining)):
                    if parameter=='schema_inference':
                        from .schema_inference import infer_schema
                        config={**params[parameter],'identity_scope':cache.partition}
                        payload[key]=await controlled(ctx,infer_schema(payload['html'],payload['final_url'],config,context=ctx))
                    else:
                        from .local_tables import extract_tables_local
                        payload[key]=await controlled(ctx,extract_tables_local(payload,payload['final_url'],params[parameter],context=ctx))
            except TimeoutError:
                payload[key]={'status':'partial','coverage':{'complete':False,'reason':'job_time_budget'}}
            except (ValueError,TypeError,ImportError) as error:
                payload[key]={'status':'partial','coverage':{'complete':False,'reason':'structure_processing_error'},'error_type':type(error).__name__}
        if params.get('llm_content_filter') is not None:
            from .local_content_filter import filter_local
            remaining=maximum_seconds-(time.monotonic()-started)
            try:
                async with asyncio.timeout(max(.001,remaining)):
                    payload['local_content_filter']=await controlled(ctx,filter_local(payload,payload['final_url'],
                        params['llm_content_filter'],context=ctx,identity_partition=cache.partition))
            except TimeoutError:
                payload['local_content_filter']={'status':'partial','coverage':{'complete':False,'reason':'job_time_budget'}}
        if params.get('semantic_extraction') is not None or params.get('chunking',{}).get('type') in {'nlp_sentence','nlp_topic'}:
            from .semantic_enrichment import enrich
            await enrich(ctx,payload)
        if params.get('link_preview_config') is not None:
            from .preview import preview_links
            remaining = maximum_seconds - (time.monotonic() - started)
            try:
                async with asyncio.timeout(max(.001, remaining)):
                    preview = await preview_links(ctx,payload,params['link_preview_config'])
                    payload['link_preview'] = {key:value for key,value in preview.items() if key!='links'}
                    payload['links'] = preview['links']
            except TimeoutError:
                payload['link_preview'] = {'status':'partial','results':[],
                    'coverage':{'complete':False,'reason':'job_time_budget'}}
        if params.get('llm_extraction') is not None:
            from .local_extraction import extract_local
            remaining = maximum_seconds - (time.monotonic() - started)
            try:
                if session.budget.reason:
                    raise TimeoutError()
                async with asyncio.timeout(max(.001, remaining)):
                    payload['llm_extraction'] = await controlled(ctx, extract_local(
                        payload, payload['final_url'], params['llm_extraction'], context=ctx))
            except TimeoutError:
                payload['llm_extraction'] = {'status': 'partial', 'records': [],
                    'coverage': {'complete': False, 'reason': 'job_time_budget'}, 'model_claim': True}
        return payload, hit

    async def preserve_sources():
        saved = await asyncio.to_thread(ctx.store.checkpoint, ctx.job_id, 'web')
        if saved is None:
            return
        cursor = copy.deepcopy(saved['cursor'])
        cursor['dispatch_state']=dispatch.checkpoint_state()
        cursor['user_agents']=copy.deepcopy(state['user_agents'])
        cursor['proxy_policies']=copy.deepcopy(state['proxy_policies'])
        pending = {entry['url']: entry for entry in cursor['pending']}
        for url,snapshot in session.snapshots.items():
            if url not in pending or url in acquired_sources:continue
            try:
                payload=await asyncio.to_thread(extract_document,snapshot['html'],snapshot['final_url'],(await capture_scope(url))[1])
            except (ValueError,TypeError):
                payload={'html':snapshot['html'],'markdown':'','links':[],'title':''}
            payload.update(snapshot,requested_url=url,render_mode='browser',cache={'status':'bypassed'},
                capture_interrupted=True)
            if network_identities.get(url):payload['network_identity']=copy.deepcopy(network_identities[url])
            classify_source(payload,(await capture_scope(url))[1],url)
            acquired_sources[url]=(pending[url],payload,False)
        recovered = []
        for url, (item, source, hit) in acquired_sources.items():
            if url not in pending:
                continue
            payload = copy.deepcopy(source)
            source_params=(await capture_scope(url))[1]
            payload.update(depth=item['depth'], parent_url=item.get('parent_url'), score=item.get('score', 0))
            for option, key in [('link_preview_config', 'link_preview'), ('llm_extraction', 'llm_extraction'),('llm_content_filter','local_content_filter'),('schema_inference','schema_inference'),('llm_tables','llm_tables'),('crawler','crawler')]:
                if source_params.get(option) is not None and key not in payload:
                    payload[key] = {'status': 'partial', 'records' if key == 'llm_extraction' else 'results': [],
                        'coverage': {'complete': False, 'reason': 'enrichment_interrupted'}}
            scope = 'pdf_pages' if payload['render_mode'] == 'pdf' else 'captured_dom' if payload['render_mode'] == 'browser' else 'response_html'
            recovered.append(Record(source='web', kind='page', native_id=url, url=payload['final_url'], payload=payload,
                coverage={'complete': False, 'scope': scope, 'depth': item['depth'], 'cached': hit,
                          'source_fetched_at': payload['source_fetched_at'], 'reason': 'enrichment_interrupted'}))
            cursor['pending'].remove(pending[url])
            if pending[url] not in cursor['failed']:
                cursor['failed'].append(pending[url])
        if recovered or any(cursor.get(key)!=saved['cursor'].get(key) for key in ('dispatch_state','user_agents','proxy_policies')):
            await asyncio.to_thread(ctx.store.commit_page, ctx.job_id, 'web', recovered,
                next_cursor=cursor, exhausted=False, expected_revision=saved['revision'],
                metadata={'reason': 'enrichment_interrupted'})

    try:
        while state["pending"] and attempts < max_pages:
            await ctx.check_control()
            if adaptive and len(adaptive.documents)>=adaptive.config.max_pages:
                reason='adaptive_page_budget'
                break
            remaining = maximum_seconds - (time.monotonic() - started)
            if remaining <= 0 or session.requests >= session.max_requests:
                reason = "time_budget" if remaining <= 0 else "request_budget"
                break
            try:
                available=await dispatch.capacity()
            except DispatchMemoryPressure:
                reason='memory_pressure'
                break
            if adaptive:
                await controlled(ctx,adaptive.rank_pending(state['pending']))
            capacity=min(concurrency,available,max_pages-attempts)
            if adaptive:capacity=min(capacity,adaptive.config.top_k_links,adaptive.config.max_pages-len(adaptive.documents))
            batch = frontier.batch(state, capacity)
            attempts += len(batch)
            admitted(batch)
            tasks = [asyncio.create_task(capture(item)) for item in batch]
            async def refill(inflight):
                nonlocal attempts,reason
                await ctx.check_control()
                if attempts>=max_pages or reason=='memory_pressure':return []
                if maximum_seconds-(time.monotonic()-started)<=0:
                    reason=reason or 'time_budget';return []
                if session.requests>=session.max_requests:
                    reason=reason or 'request_budget';return []
                if adaptive:
                    if reason and reason.startswith('adaptive_'):return []
                    adaptive_reason=await controlled(ctx,adaptive.decision(state['pending']))
                    if adaptive_reason:
                        reason=reason or adaptive_reason;return []
                slots=min(concurrency-len(inflight),max_pages-attempts)
                if adaptive:slots=min(slots,adaptive.config.top_k_links-len(inflight),adaptive.config.max_pages-len(adaptive.documents)-len(inflight))
                if slots<=0 or frontier.kind=='dfs' and inflight:return []
                try:
                    available=await dispatch.capacity(wait=not inflight)
                except DispatchMemoryPressure:
                    reason='memory_pressure';return []
                if not available:return []
                busy={urlsplit(item['url']).hostname or urlsplit(item['url']).scheme for item in inflight}
                candidates=[item for item in state['pending'] if (urlsplit(item['url']).hostname or urlsplit(item['url']).scheme) not in busy]
                if frontier.kind=='bfs' and state['pending']:
                    depth=min(item['depth'] for item in state['pending'])
                    candidates=[item for item in candidates if item['depth']==depth]
                    candidates.sort(key=lambda item:fair_hosts.get(urlsplit(item['url']).hostname or urlsplit(item['url']).scheme,0))
                selected=frontier.batch({**state,'pending':candidates},min(slots,available))
                admitted(selected);attempts+=len(selected)
                additions=[(item,asyncio.create_task(capture(item))) for item in selected]
                tasks.extend(task for _,task in additions)
                return additions
            iterator=results_in_order(batch,tasks,stream=stream,refill=refill if stream else None)
            try:
                # Explicit stream mode publishes completed pages immediately.
                # Pending URLs remain durable until their own atomic commit.
                async for item, task in iterator:
                    url, records = item["url"], []
                    try:
                        payload, hit = await task
                        hits += hit
                        payload.update(depth=item["depth"], parent_url=item.get("parent_url"), score=item.get("score", 0))
                        content_bytes += len(payload["html"].encode("utf-8"))
                        incomplete = bool(session.blocked_requests or payload.get("pdf_truncated") or not payload.get('dom_coverage',{}).get('complete',True)
                                          or payload.get('llm_extraction',{}).get('status','complete') != 'complete'
                                        or payload.get('link_preview',{}).get('status','complete') != 'complete'
                                        or payload.get('c4a_result',{}).get('success',True) is False
                                        or not payload.get('browser_evidence',{}).get('coverage',{}).get('complete',True)
                                        or any(item['status']!='complete' for item in payload.get('downloads',[])))
                        incomplete = incomplete or payload.get('semantic_extraction',{}).get('status','complete')!='complete' or payload.get('nlp_chunking',{}).get('status','complete')!='complete'
                        incomplete = incomplete or payload.get('tls',{}).get('status') == 'unavailable'
                        incomplete = incomplete or payload.get('local_content_filter',{}).get('status','complete') != 'complete'
                        incomplete = incomplete or any(payload.get(key,{}).get('status','complete')!='complete' for key in ('schema_inference','llm_tables'))
                        incomplete = incomplete or payload.get('crawler',{}).get('status','complete')!='complete'
                        incomplete = incomplete or payload.get('screenshot',{}).get('status','complete')!='complete'
                        incomplete = incomplete or payload.get('source_usable') is False
                        incomplete = incomplete or bool(payload.get('text_decoding',{}).get('replacement_characters'))
                        incomplete = incomplete or payload.get('javascript_execution',{}).get('success',True) is False or bool(payload.get('hook_failure'))
                        scope = "pdf_pages" if payload["render_mode"] == "pdf" else "captured_dom" if payload["render_mode"] == "browser" else "response_html"
                        record = Record(source="web", kind="page", native_id=url, url=payload["final_url"], payload=payload,
                            coverage={"complete": not incomplete, "scope": scope, "depth": item["depth"],
                                      "blocked_budget_requests": session.blocked_requests,
                                      "cached": hit, "source_fetched_at": payload["source_fetched_at"]})
                        records = [record]
                        state["pending"].remove(item)
                        if incomplete:
                            state["failed"].append(item)
                            reason = "pdf_page_budget" if payload.get("pdf_truncated") else 'dom_coverage_limit' if not payload.get('dom_coverage',{}).get('complete',True) else 'llm_extraction_partial' if payload.get('llm_extraction',{}).get('status','complete') != 'complete' else 'link_preview_partial' if payload.get('link_preview',{}).get('status','complete') != 'complete' else "request_budget"
                            if payload.get('c4a_result',{}).get('success',True) is False:
                                reason = 'c4a_' + payload['c4a_result']['status']
                            elif not payload.get('browser_evidence',{}).get('coverage',{}).get('complete',True):
                                reason = 'browser_observation_limit'
                            elif any(item['status']!='complete' for item in payload.get('downloads',[])):
                                reason = 'browser_download_incomplete'
                            elif payload.get('semantic_extraction',{}).get('status','complete')!='complete' or payload.get('nlp_chunking',{}).get('status','complete')!='complete':
                                reason = 'semantic_processing_partial'
                            elif payload.get('tls',{}).get('status') == 'unavailable':
                                reason = 'tls_certificate_unavailable'
                            elif payload.get('local_content_filter',{}).get('status','complete') != 'complete':
                                reason = 'local_content_filter_partial'
                            elif any(payload.get(key,{}).get('status','complete')!='complete' for key in ('schema_inference','llm_tables')):
                                reason = 'local_structure_partial'
                            elif payload.get('javascript_execution',{}).get('success',True) is False or payload.get('hook_failure'):
                                reason = 'browser_script_or_hook_partial'
                            elif payload.get('crawler',{}).get('status','complete')!='complete':
                                reason = 'crawler_extraction_partial'
                            elif payload.get('screenshot',{}).get('status','complete')!='complete':
                                reason = 'screenshot_coverage_limit'
                            elif payload.get('source_usable') is False:
                                reason = 'suspected_block' if payload.get('blocking',{}).get('blocked') else 'http_error_response'
                                record.coverage['reason']=reason
                            elif payload.get('text_decoding',{}).get('replacement_characters'):
                                reason = 'text_decoding_lossy'
                                record.coverage['reason']=reason
                        elif url not in state["visited"]:
                            state["visited"].append(url)
                        if not session.budget.reason and payload.get('source_usable') is not False:
                            await controlled(ctx, frontier.discover(state, item, payload))
                        elif session.budget.reason:
                            reason = session.budget.reason
                        reason = state.get("coverage_limit") or reason
                        if adaptive:
                            await controlled(ctx,adaptive.observe(url,payload,source_complete=not incomplete))
                            await controlled(ctx,adaptive.rank_pending(state['pending']))
                            state['adaptive']=adaptive.dump()
                            payload['adaptive']=adaptive.summary()
                            if adaptive_limits():
                                reason=reason or adaptive_limits()[0]
                                record.coverage['complete']=False
                                record.coverage['reason']=adaptive_limits()[0]
                        records = [record]
                    except DispatchMemoryPressure:
                        reason='memory_pressure'
                        break
                    except (httpx.HTTPError, *BrowserError, ValueError, TimeoutError, LimitReached, OSError, ImportError) as error:
                        reason=session.budget.reason or reason
                        if item in state["pending"]:
                            state["pending"].remove(item)
                        if url in state["visited"]:
                            state["visited"].remove(url)
                        state["failed"].append(item)
                        errors.append({"url": url, "type": type(error).__name__})
                    state['dispatch_state']=dispatch.checkpoint_state()
                    exhausted = not state["pending"] and not state["failed"] and not session.blocked_requests and reason is None and not adaptive_limits()
                    checkpoint = await ctx.commit_page("web", records, next_cursor=state, exhausted=exhausted,
                        expected_revision=revision, metadata={"attempts_this_run": attempts, "errors": errors, "reason": reason})
                    revision = checkpoint["revision"]
                    acquired_sources.pop(url, None)
                    session.snapshots.pop(url,None)
            finally:
                await iterator.aclose()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if reason=='memory_pressure':
                break
            if adaptive:
                adaptive_reason=await controlled(ctx,adaptive.decision(state['pending']))
                state['adaptive']=adaptive.dump()
                checkpoint=await ctx.commit_page('web',[],next_cursor=state,
                    exhausted=not state['pending'] and not state['failed'] and not session.blocked_requests and reason is None and adaptive_reason is None,
                    expected_revision=revision,metadata={'adaptive':adaptive.summary()})
                revision=checkpoint['revision']
                if adaptive_reason:
                    reason=reason or adaptive_reason
                    break
    except (asyncio.CancelledError, JobControl, BudgetExceeded):
        # Optional enrichment must not discard an acquired source when the
        # outer JobRunner deadline/cancel wins over capture's inner deadline.
        # Inner batch cleanup has already settled all capture tasks. Reload the
        # committed cursor: commit_page can commit successfully then raise a
        # control signal, so the in-memory revision is not authoritative here.
        preserving = asyncio.create_task(preserve_sources())
        while not preserving.done():
            try:
                await asyncio.shield(preserving)
            except asyncio.CancelledError:
                pass
        preserving.result()
        raise
    finally:
        head_fetcher.reset(token)
    if reason == "memory_pressure":
        await preserve_sources()
        saved = await ctx.checkpoint("web")
        state = saved["cursor"]
    if adaptive_limits():
        reason=reason or adaptive_limits()[0]
        adaptive.last_reason=reason
    complete = not state["pending"] and not state["failed"] and not session.blocked_requests and reason is None
    return {"status": "complete" if complete else "partial", "pages": len(state["visited"]),
            "attempts": attempts, "pending": len(state["pending"]), "failed": len(state["failed"]),
            "reason": reason or ("page_budget" if state["pending"] else "page_errors" if state["failed"] else "request_budget" if session.blocked_requests else None),
            "errors": errors, "requests": session.requests, "html_bytes": content_bytes, "network_bytes": session.budget.bytes,
            "cache_hits": hits, "strategy": frontier.kind, "concurrency": concurrency,
            "dispatch":dispatch.stats(),
            "publication_order": "completion" if stream else "admission",
            "stream_refill": stream,
            "adaptive": adaptive.summary() if adaptive else None,
            "elapsed_seconds": round(time.monotonic() - started, 3), "browser_launches":len({id(browser) for candidate in [session,*[scope[2] for scope in capture_scopes.values()]] for browser in candidate.launched_browsers}),
            "run_configurations":len(capture_scopes)}
