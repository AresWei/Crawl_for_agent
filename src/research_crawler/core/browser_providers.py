"""Source-derived browser adapters and unified provider selection.

BrowserAdapter/PlaywrightAdapter from Crawl4AI (Apache-2.0), commit
838869bdda93f25e6d50b3f50ae9577d9b19a586; see licenses and
 docs/audits/browser-providers-manifest.json. Provider lifecycle and budget
bridge are integration code. No challenge handling or stealth injection.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Callable
import time
import json

# Import both, but use conditionally
try:
    from playwright.async_api import Page
except ImportError:
    Page = Any



class BrowserAdapter(ABC):
    """Abstract adapter for browser-specific operations"""
    
    @abstractmethod
    async def evaluate(self, page: Page, expression: str, arg: Any = None) -> Any:
        """Execute JavaScript in the page"""
        pass
    
    @abstractmethod
    async def setup_console_capture(self, page: Page, captured_console: List[Dict]) -> Optional[Callable]:
        """Setup console message capturing, returns handler function if needed"""
        pass
    
    @abstractmethod
    async def setup_error_capture(self, page: Page, captured_console: List[Dict]) -> Optional[Callable]:
        """Setup error capturing, returns handler function if needed"""
        pass
    
    @abstractmethod
    async def retrieve_console_messages(self, page: Page) -> List[Dict]:
        """Retrieve captured console messages (for undetected browsers)"""
        pass
    
    @abstractmethod
    async def cleanup_console_capture(self, page: Page, handle_console: Optional[Callable], handle_error: Optional[Callable]):
        """Clean up console event listeners"""
        pass
    
    @abstractmethod
    def get_imports(self) -> tuple:
        """Get the appropriate imports for this adapter"""
        pass


class PlaywrightAdapter(BrowserAdapter):
    """Adapter for standard Playwright"""
    
    async def evaluate(self, page: Page, expression: str, arg: Any = None) -> Any:
        """Standard Playwright evaluate"""
        if arg is not None:
            return await page.evaluate(expression, arg)
        return await page.evaluate(expression)
    
    async def setup_console_capture(self, page: Page, captured_console: List[Dict]) -> Optional[Callable]:
        """Setup console capture using Playwright's event system"""
        def handle_console_capture(msg):
            try:
                message_type = "unknown"
                try:
                    message_type = msg.type
                except:
                    pass
                    
                message_text = "unknown"
                try:
                    message_text = msg.text
                except:
                    pass
                    
                entry = {
                    "type": message_type,
                    "text": message_text,
                    "timestamp": time.time()
                }
                
                captured_console.append(entry)
                
            except Exception as e:
                captured_console.append({
                    "type": "console_capture_error", 
                    "error": str(e), 
                    "timestamp": time.time()
                })
        
        page.on("console", handle_console_capture)
        return handle_console_capture
    
    async def setup_error_capture(self, page: Page, captured_console: List[Dict]) -> Optional[Callable]:
        """Setup error capture using Playwright's event system"""
        def handle_pageerror_capture(err):
            try:
                error_message = "Unknown error"
                try:
                    error_message = err.message
                except:
                    pass
                    
                error_stack = ""
                try:
                    error_stack = err.stack
                except:
                    pass
                    
                captured_console.append({
                    "type": "error",
                    "text": error_message,
                    "stack": error_stack,
                    "timestamp": time.time()
                })
            except Exception as e:
                captured_console.append({
                    "type": "pageerror_capture_error", 
                    "error": str(e), 
                    "timestamp": time.time()
                })
        
        page.on("pageerror", handle_pageerror_capture)
        return handle_pageerror_capture
    
    async def retrieve_console_messages(self, page: Page) -> List[Dict]:
        """Not needed for Playwright - messages are captured via events"""
        return []
    
    async def cleanup_console_capture(self, page: Page, handle_console: Optional[Callable], handle_error: Optional[Callable]):
        """Remove event listeners"""
        if handle_console:
            page.remove_listener("console", handle_console)
        if handle_error:
            page.remove_listener("pageerror", handle_error)
    
    def get_imports(self) -> tuple:
        """Return Playwright imports"""
        from playwright.async_api import Page, Error
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        return Page, Error, PlaywrightTimeoutError


class PatchrightAdapter(PlaywrightAdapter):
    """Source-derived ordinary-page console collection, bounded in-page.

    Unlike the upstream string heuristic, user JavaScript consistently executes
    in the main world. This adapter does not interact with challenge widgets.
    """
    async def evaluate(self, page, expression, arg=None):
        return await page.evaluate(expression, arg, isolated_context=False)

    async def _setup(self, page):
        script = """(() => {
          if(window.__rcCapture) return;
          const state=window.__rcCapture={messages:[],omitted:0};
          const push=(type,text)=>{
            if(state.messages.length>=500){state.omitted++;return;}
            state.messages.push({type,text:String(text).slice(0,8192),timestamp:Date.now()/1000});
          };
          for(const type of ['log','info','warn','error','debug']){
            const original=console[type];
            console[type]=function(...args){
              push(type,args.map(x=>{try{return typeof x==='object'?JSON.stringify(x):String(x)}catch(e){return '[Object]'}}).join(' '));
              return original.apply(console,args);
            };
          }
          window.addEventListener('error',e=>push('error',e.message));
          window.addEventListener('unhandledrejection',e=>push('unhandledrejection',e.reason));
        })();"""
        await page.add_init_script(script)
        await self.evaluate(page, script)

    async def setup_console_capture(self, page, captured_console):
        await self._setup(page)
        return None

    async def setup_error_capture(self, page, captured_console):
        # Same idempotent initializer fixes the original mutually-exclusive flags.
        await self._setup(page)
        return None

    async def retrieve_console_messages(self, page):
        return await self.evaluate(page, """() => {
          const s=window.__rcCapture;if(!s)return [];
          const out=s.messages.splice(0);
          if(s.omitted){out.push({type:'capture_omitted',text:String(s.omitted),timestamp:Date.now()/1000});s.omitted=0;}
          return out;
        }""")

    async def cleanup_console_capture(self, page, handle_console, handle_error):
        return [] if page.is_closed() else await self.retrieve_console_messages(page)

    def get_imports(self):
        from patchright.async_api import Page, Error, TimeoutError
        return Page, Error, TimeoutError


def validate_provider_options(params, *, accept_downloads=False, context_options=None, launch_args=None):
    """Reject unavailable combinations before browser launch or navigation."""
    engine = params.get('browser_type', 'chromium')
    provider = params.get('browser_provider', 'playwright')
    if engine not in {'chromium', 'firefox', 'webkit'}:
        raise ValueError('browser_type must be chromium, firefox or webkit')
    if provider not in {'playwright', 'patchright'}:
        raise ValueError('browser_provider must be playwright or patchright')
    if params.get('enable_stealth') or params.get('use_undetected'):
        raise ValueError('Select browser_provider explicitly; stealth/challenge handling is not enabled')
    if provider == 'patchright' and engine != 'chromium':
        raise ValueError('Patchright supports Chromium only')
    if engine != 'chromium':
        unsupported = ('cdp_url', 'channel', 'browser_profile', 'pdf', 'mhtml', 'fetch_ssl_certificate', 'accept_downloads')
        if any(params.get(key) for key in unsupported) or accept_downloads:
            raise ValueError('CDP/channel/profile/PDF/MHTML/certificate/downloads require Chromium')
        if launch_args or params.get('launch_args'):
            raise ValueError('Custom launch flags currently require Chromium')
        options = context_options or params.get('browser_options') or {}
        if engine == 'firefox' and options.get('is_mobile'):
            raise ValueError('Unsupported context option for the budgeted non-Chromium transport')
    return provider, engine


def provider_runtime(provider):
    # The conditional import and engine dispatch are adapted from the original
    # BrowserManager; the core AsyncExitStack owns the sole selected runtime.
    if provider == 'patchright':
        try:
            from patchright.async_api import async_playwright
        except ImportError as error:
            raise RuntimeError('Optional Patchright provider is not installed') from error
    else:
        from playwright.async_api import async_playwright
    return async_playwright()


def browser_error_types():
    """Both public providers use distinct Error classes; preserve one catch seam."""
    from playwright.async_api import Error
    try:
        from patchright.async_api import Error as PatchrightError
    except ImportError:
        return (Error,)
    return (Error, PatchrightError)


class NonCdpBudgetBridge:
    """Native cross-engine request/response accounting without body copies.

    Cross-engine Playwright has no incremental encoded-byte event. Completed
    responses are charged using Request.sizes(); headers permit early rejection
    of known-length bodies. Unknown/chunked in-flight bodies can overshoot by
    their entire transfer. This is explicitly a soft network-byte budget.
    """
    def __init__(self, context, browser_context, budget):
        self.context, self.browser_context, self.budget = context, browser_context, budget
        self.tasks = set()
        self.listeners = []
        self.closed = False
        self.responses = 0
        self.errors = 0
        self.pending = set()

    async def start(self):
        self.context.resources.push_async_callback(self.close)
        self.listen('response', self.response)
        self.listen('requestfinished', self.finished)
        self.listen('requestfailed', self.failed)
        return self

    def listen(self, event, callback):
        self.browser_context.on(event, callback)
        self.listeners.append((event, callback))

    def spawn(self, awaitable):
        import asyncio
        if self.closed:
            awaitable.close()
            return
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def stop(self):
        # Closing all owned pages stops in-flight responses without CDP.
        import asyncio
        await asyncio.gather(*(page.close() for page in self.browser_context.pages), return_exceptions=True)

    def response(self, response):
        self.pending.add(response.request)
        self.spawn(self.headers(response))

    def finished(self, request):
        self.pending.discard(request)
        self.spawn(self.charge_response(request))

    def failed(self, request):
        had_response = request in self.pending
        self.pending.discard(request)
        if had_response and not self.closed and not self.budget.reason:
            self.errors += 1
            self.budget.reason = 'browser_byte_measurement_unavailable'

    async def headers(self, response):
        try:
            headers = await response.all_headers()
            if 'text/event-stream' in headers.get('content-type', ''):
                self.budget.reason = 'unsupported_streaming_response'
                await self.stop()
                return
            try:
                length = int(headers.get('content-length', '0'))
            except ValueError:
                length = 0
            if response.request.method == 'HEAD' or response.status in {204, 304}:
                length = 0
            if length > self.budget.maximum_bytes - self.budget.bytes:
                self.budget.reason = 'network_byte_budget'
                await self.stop()
                return
        except Exception:
            self.errors += 1
            self.budget.reason = self.budget.reason or 'browser_byte_measurement_unavailable'
            await self.stop()

    async def charge_response(self, request):
        from .jobs import BudgetExceeded
        try:
            sizes = await request.sizes()
            self.responses += 1
            await self.budget.received(max(0, int(sizes['responseBodySize'])))
        except BudgetExceeded:
            await self.stop()
        except Exception:
            self.errors += 1
            # Do not silently treat unavailable byte measurements as zero.
            self.budget.reason = self.budget.reason or 'browser_byte_measurement_unavailable'
            await self.stop()

    async def settle(self):
        import asyncio
        deadline = asyncio.get_running_loop().time() + min(60, float(self.context.params.get('timeout_seconds', 30)))
        while self.tasks or self.pending:
            await self.context.check_control()
            if asyncio.get_running_loop().time() >= deadline:
                self.budget.reason = 'browser_byte_measurement_timeout'
                await self.context.check_control()
            # Do not await Playwright Response.finished(): cancelling its waiter
            # leaves a TargetClosed future in some provider implementations.
            # Events provide completion without retaining that internal future.
            await asyncio.sleep(.01)
        await self.context.check_control()

    async def close(self):
        import asyncio
        self.closed = True
        for event, callback in self.listeners:
            self.browser_context.remove_listener(event, callback)
        self.listeners.clear()
        self.pending.clear()
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def evidence(self):
        return {'mode':'native_request_response_sizes', 'bytes':'encoded_response_body',
            'accounting_scope':'completed_native_http_response_bodies; excludes headers, TLS overhead and unavailable failed-response bytes',
            'budget_enforcement':'response_completion_soft_limit', 'responses_measured':self.responses,
            'measurement_errors':self.errors, 'body_buffer_allocated':False,
            'overshoot':'Unknown-length responses are charged after completion; an entire response and concurrent in-flight transfers may exceed the cap. Known Content-Length is checked at headers. No finite wire-byte overshoot bound is claimed.',
            'limitations':['service_workers', 'websockets', 'event_streams', 'incremental_wire_byte_limit']}
