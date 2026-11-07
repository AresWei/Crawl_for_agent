"""One owner for HTTP clients, browser sessions, budgets and selected identities."""
from __future__ import annotations

import asyncio
import json
import time
import weakref
from dataclasses import dataclass
from functools import wraps
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import unquote, urlsplit

import httpx

from .accounts import AccountStore, ProxyStore
from .jobs import BudgetExceeded, JobContext, JobControl
from .browser_providers import (validate_provider_options, provider_runtime,
    PlaywrightAdapter, PatchrightAdapter, NonCdpBudgetBridge)


async def controlled(context: JobContext, awaitable):
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=.2)
            await context.check_control()
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class NetworkBudget:
    def __init__(self, context: JobContext):
        self.context = context
        params = context.params
        self.maximum_requests = max(1, min(100000, int(params.get("max_requests", 2000))))
        self.maximum_bytes = max(1024, min(2_000_000_000, int(params.get("max_network_bytes", 100_000_000))))
        self.maximum_seconds = max(1, min(7200, float(params.get("max_seconds", 600))))
        self.started = time.monotonic()
        self.requests = self.bytes = self.blocked = 0
        self.reason = None
        self._browser_requests = weakref.WeakSet()

    async def before_browser_request(self, request):
        if request in self._browser_requests:
            return
        self._browser_requests.add(request)
        await self.before_request()

    async def before_request(self, request=None):
        await self.context.check_control()
        reason = self.reason or ("time_budget" if time.monotonic() - self.started >= self.maximum_seconds else "network_byte_budget" if self.bytes >= self.maximum_bytes else "request_budget" if self.requests >= self.maximum_requests else None)
        if reason:
            self.reason = reason
            self.blocked += 1
            raise BudgetExceeded(reason)
        self.requests += 1

    async def received(self, size):
        self.bytes += size
        if self.bytes > self.maximum_bytes:
            self.reason = "network_byte_budget"
            raise BudgetExceeded(self.reason)


def budget_for(context: JobContext) -> NetworkBudget:
    if not hasattr(context, "_network_budget"):
        context._network_budget = NetworkBudget(context)
    return context._network_budget


class BudgetStream(httpx.AsyncByteStream):
    def __init__(self, wrapped, budget):
        self.wrapped, self.budget = wrapped, budget

    async def __aiter__(self):
        iterator = self.wrapped.__aiter__()
        while True:
            try:
                # A stream=True request returns before body bytes arrive. Keep
                # quiet body reads cancellable, not only the request headers.
                part = await controlled(self.budget.context, anext(iterator))
            except StopAsyncIteration:
                break
            await self.budget.received(len(part))
            yield part

    async def aclose(self):
        await self.wrapped.aclose()


class BudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped, budget):
        self.wrapped, self.budget = wrapped, budget

    async def handle_async_request(self, request):
        response = await self.wrapped.handle_async_request(request)
        response.stream = BudgetStream(response.stream, self.budget)
        return response

    async def aclose(self):
        await self.wrapped.aclose()


class ScopedClient(httpx.AsyncClient):
    def __init__(self, context, **kwargs):
        self.job_context = context
        super().__init__(**kwargs)

    def _merge_cookies(self, cookies=None):
        # HTTPX copies Cookies into a fresh permissive jar while building each
        # request. Return a CookieJar so the exact-host policy survives that copy.
        merged = super()._merge_cookies(cookies)
        if merged is None:
            return None
        jar = httpx.Cookies(merged).jar
        jar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
        return jar

    def _build_redirect_request(self, request, response):
        redirected = super()._build_redirect_request(request, response)
        # Redirect construction also makes a fresh HTTPX cookie copy.
        redirected.headers.pop("cookie", None)
        self.cookies.set_cookie_header(redirected)
        return redirected

    async def send(self, request, **kwargs):
        return await controlled(self.job_context, super().send(request, **kwargs))


async def open_http(context: JobContext, *, platform="web", account=None, headers=None,
                    transport=None, require_account=False, timeout=None, verify=None) -> httpx.AsyncClient:
    if verify is None:
        verify = context.params.get('verify_ssl', True)
    if type(verify) is not bool:
        raise ValueError('HTTP TLS verification must be a boolean')
    from .account_pool import pinned_identity, FrozenAccounts
    identity = pinned_identity(context, platform, account)
    cookies = (FrozenAccounts(identity) if identity else AccountStore(context.paths)).cookies(platform, account, required=require_account)
    proxy = identity["proxy_url"] if identity else ProxyStore(context.paths).get(context.params.get("proxy_id"))
    budget = budget_for(context)
    effective_timeout=timeout or min(60,float(context.params.get('timeout_seconds',30)))
    key = (platform, account, tuple(sorted((headers or {}).items())), id(transport), context.params.get("proxy_id"),effective_timeout,verify)
    cache = getattr(context, "_http_clients", {})
    if key in cache:
        return cache[key]
    limits = httpx.Limits(max_connections=2 if platform == "web" else 1, max_keepalive_connections=1)
    underlying = transport or httpx.AsyncHTTPTransport(proxy=proxy, limits=limits, retries=0,verify=verify,trust_env=False)
    client = ScopedClient(context, cookies=cookies.jar, headers=headers, trust_env=False,
        follow_redirects=False, timeout=effective_timeout,
        transport=BudgetTransport(underlying, budget), event_hooks={"request": [budget.before_request]})
    client = await context.resources.enter_async_context(client)
    cache[key] = client
    context._http_clients = cache
    return client


@dataclass
class BrowserSession:
    browser: object
    context: object
    page: object
    budget: NetworkBudget
    page_owned: bool = True
    downloads: object = None
    engine: str = 'chromium'
    provider: str = 'playwright'
    adapter: object = None
    network_bridge: object = None
    browser_modes: dict | None = None


class ManagedBrowserContext:
    """Keep instrumentation ready before callers can navigate a new page."""

    def __init__(self, context, watch_page):
        self._context, self._watch_page = context, watch_page

    def __getattr__(self, name):
        return getattr(self._context, name)

    async def new_page(self):
        page = await self._context.new_page()
        try:
            await self._watch_page(page)
        except BaseException:
            await page.close()
            raise
        return page


def _serialize_browser_open(function):
    @wraps(function)
    async def wrapper(context, *args, **kwargs):
        # Concurrent web pages share one job-owned launch and profile lease.
        # Initialization contains no await, so all callers observe this lock.
        if not hasattr(context, "_browser_open_lock"):
            context._browser_open_lock = asyncio.Lock()
        async with context._browser_open_lock:
            return await function(context, *args, **kwargs)
    return wrapper


@_serialize_browser_open
async def open_browser(context: JobContext, *, platform="web", account=None, headless=True,
                       init_scripts=None, headers=None, require_account=False, storage_state=None,
                       viewport=None, manage_budget=True, block_media=None,
                       context_options=None, launch_args=None, accept_downloads=False,
                       on_browser_created=None) -> BrowserSession:
    provider, engine = validate_provider_options(context.params, accept_downloads=accept_downloads,
        context_options=context_options, launch_args=launch_args)
    from .browser_recycle import recycle_limit, BrowserGenerations
    recycle = recycle_limit(context.params)
    from .browser_behavior import behavior_options, NAVIGATOR_SCRIPT
    if behavior_options(context.params):
        init_scripts = list(dict.fromkeys([*(init_scripts or []), NAVIGATOR_SCRIPT]))
    if engine != 'chromium' and not manage_budget:
        raise ValueError('Non-Chromium browsers require shared network budgets')
    if type(accept_downloads) is not bool:
        raise ValueError("accept_downloads must be boolean")
    if accept_downloads and (context.params.get("cdp_url") or not manage_budget):
        raise ValueError("Downloads require an owned browser with shared network budgets; external CDP downloads are unsupported")
    profile_name = context.params.get("browser_profile")
    if profile_name and (context.params.get("cdp_url") or storage_state is not None or require_account):
        raise ValueError("Named profiles require owned browsing without storage-state import or account-required mode")
    options = dict(context_options or {})
    allowed = {"locale", "timezone_id", "geolocation", "permissions", "user_agent", "ignore_https_errors",
               "color_scheme", "java_script_enabled", "device_scale_factor", "is_mobile", "has_touch",
               "reduced_motion", "offline", "base_url", "bypass_csp"}
    if set(options) - allowed:
        raise ValueError("Unsupported browser context option")
    if launch_args and (not isinstance(launch_args, list) or any(not isinstance(value, str) or not value.startswith("--") for value in launch_args)):
        raise ValueError("Browser launch arguments must be a list of flags")
    if any(value.startswith(("--remote-debugging", "--user-data-dir", "--load-extension", "--proxy-server")) for value in launch_args or []):
        raise ValueError("Use managed browser/account/proxy settings instead of overriding their launch flags")
    if profile_name and any(value.startswith(("--profile-directory", "--disk-cache-dir", "--crash-dumps-dir", "--log-file")) for value in launch_args or []):
        raise ValueError("Named profiles keep all browser storage inside the application home")
    from .browser_modes import browser_modes
    modes = browser_modes(context.params, engine=engine, manage_budget=manage_budget,
        account=account, storage_state=storage_state, context_options=options)
    from .account_pool import pinned_identity, FrozenAccounts
    identity = pinned_identity(context, platform, account)
    if identity and storage_state is not None:
        raise ValueError("Pool browsing uses its pinned account state")
    state = FrozenAccounts(identity).state(platform, account) if identity else None if profile_name else storage_state if storage_state is not None else AccountStore(context.paths).state(platform, account, required=require_account)
    mode_receipt = modes.receipt()
    if modes.active:
        launch_args = list(dict.fromkeys([*(launch_args or []), *modes.flags]))
        options['service_workers'] = 'block'
    if modes.text:
        options.update(java_script_enabled=False, has_touch=False, is_mobile=False)
    key = (platform, account, headless, tuple(init_scripts or ()), tuple(sorted((headers or {}).items())),
           json.dumps(options, sort_keys=True), json.dumps(viewport, sort_keys=True), tuple(launch_args or ()), manage_budget, block_media, accept_downloads,
           context.params.get('cdp_url'), context.params.get('target_id'), context.params.get('browser_context_id'), profile_name,
           provider, engine, context.params.get('proxy_id'), modes, recycle, id(on_browser_created) if on_browser_created else None)
    cache = getattr(context, "_browser_sessions", {})
    if key in cache:
        return cache[key]
    if recycle:
        from contextlib import AsyncExitStack
        shared_budget = budget_for(context)
        frozen_params = dict(context.params)
        async def generation():
            # ParamsContext forwards attribute assignment to its parent: never
            # shallow-copy it and overwrite the shared session cache/lock.
            child = JobContext(context.job_id, {**frozen_params, 'max_pages_before_recycle': 0},
                               context.store, context.paths, AsyncExitStack())
            child._network_budget = shared_budget
            try:
                session = await open_browser(child, platform=platform, account=account, headless=headless,
                    init_scripts=init_scripts, headers=headers, require_account=require_account,
                    storage_state=storage_state, viewport=viewport, manage_budget=manage_budget,
                    block_media=block_media, context_options=context_options, launch_args=launch_args,
                    accept_downloads=accept_downloads, on_browser_created=on_browser_created)
                return session, child.resources
            except BaseException:
                await child.resources.aclose()
                raise
        pool = BrowserGenerations(context, recycle, generation)
        context.resources.push_async_callback(pool.close)
        await pool.start()
        cache[key] = pool
        context._browser_sessions = cache
        return pool
    profile_path = None
    if profile_name:
        from .browser_profiles import BrowserProfileStore
        profile_path = context.resources.enter_context(BrowserProfileStore(context.paths).acquire(
            profile_name, platform=platform, account=account, job_id=context.job_id))
    if context.params.get('cdp_url') and context.params.get('target_id'):
        from .cdp import lease_target
        await lease_target(context,context.params['target_id'])
    playwright = await context.resources.enter_async_context(provider_runtime(provider))
    browser_type = getattr(playwright, engine)
    launch = {"headless": headless, "downloads_path": str(context.paths.root / "temp"), "env": context.paths.child_environment()}
    if launch_args:
        launch["args"] = launch_args
    channel = context.params.get("channel")
    if channel:
        if channel not in {"chrome", "chromium"}:
            raise ValueError("Only Chrome/Chromium channels are configured")
        launch["channel"] = channel
    proxy = identity["proxy_url"] if identity else ProxyStore(context.paths).get(context.params.get("proxy_id"))
    if proxy:
        parts = urlsplit(proxy)
        launch["proxy"] = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
        if parts.username:
            launch["proxy"].update(username=unquote(parts.username), password=unquote(parts.password or ""))
    external = context.params.get('cdp_url')
    selected_page = None
    browser_context = None
    if headers and "User-Agent" in headers and "user_agent" not in options:
        options["user_agent"] = headers["User-Agent"]
    if external:
        if proxy or launch_args or channel:
            raise ValueError('An external browser keeps its launch/proxy/channel configuration')
        from .cdp import connect
        browser, browser_context, selected_page = await connect(playwright, context.params, context)
    elif profile_path is not None:
        browser_context = await browser_type.launch_persistent_context(str(profile_path),
            extra_http_headers=headers, viewport=viewport or {"width": 1440, "height": 1000},
            accept_downloads=accept_downloads, **options, **launch)
        browser = browser_context.browser
        context.resources.push_async_callback(browser_context.close)
        # Start with a new owned page; never restore live pages from earlier jobs.
        for initial_page in list(browser_context.pages):
            await initial_page.close()
    else:
        browser = await browser_type.launch(**launch)
        context.resources.push_async_callback(browser.close)
    borrowed = browser_context is not None and profile_path is None
    if borrowed and (account or storage_state or options or headers or init_scripts or viewport or proxy):
        raise ValueError('An existing CDP context keeps its identity/configuration; omit context overrides')
    if not borrowed and profile_path is None:
        # Routed network budgets cannot observe service-worker-owned fetches.
        if engine != 'chromium':
            options['service_workers'] = 'block'
        browser_context = await browser.new_context(storage_state=state, extra_http_headers=headers,
            viewport=viewport or {"width": 1440, "height": 1000}, accept_downloads=accept_downloads, **options)
        context.resources.push_async_callback(browser_context.close)
    for script in init_scripts or []:
        await browser_context.add_init_script(script=script)
    budget = budget_for(context)
    bridge = await NonCdpBudgetBridge(context, browser_context, budget).start() if engine != 'chromium' else None
    adapter = PatchrightAdapter() if provider == 'patchright' else PlaywrightAdapter()
    downloads = None
    tracking, stop_tasks, cdp_sessions, request_listeners = {}, [], [], []
    tracking_active = True
    async def watch_page(page):
        async def attach():
            if manage_budget:
                async def charge_redirect(request):
                    try:
                        await budget.before_browser_request(request)
                    except (BudgetExceeded, JobControl):
                        if not page.is_closed():
                            await page.close()
                def request_event(request):
                    if request.redirected_from is not None:
                        stop_tasks.append(asyncio.create_task(charge_redirect(request)))
                page.on('request', request_event)
                request_listeners.append((page, request_event))
            if downloads is not None:
                await downloads.watch_page(page)
            if borrowed:
                if manage_budget:
                    await page.route('**/*', route_request)
                    async def release_page_route():
                        if not page.is_closed():
                            await page.unroute('**/*', route_request)
                    context.resources.push_async_callback(release_page_route)
                if page != selected_page:
                    context.resources.push_async_callback(page.close)
            if not manage_budget or engine != 'chromium':
                return
            cdp = await browser_context.new_cdp_session(page)
            cdp_sessions.append(cdp)
            seen = {}
            async def stop_loading():
                try:
                    await cdp.send("Page.stopLoading")
                except Exception:
                    pass  # Closing a timed-out page may have already detached CDP.
            def receive(request_id, count, *, final=False):
                if not tracking_active:
                    return
                before = seen.get(request_id, 0)
                amount = max(0, count - before) if final else max(0, count)
                seen[request_id] = before + amount
                if downloads is not None:
                    downloads.network_received(page, request_id, seen[request_id])
                else:
                    budget.bytes += int(amount)
                if final:
                    seen.pop(request_id, None)
                if budget.bytes > budget.maximum_bytes and budget.reason is None:
                    budget.reason = "network_byte_budget"
                    stop_tasks.append(asyncio.create_task(stop_loading()))
            cdp.on("Network.dataReceived", lambda event: receive(event["requestId"], event.get("encodedDataLength", 0)))
            cdp.on("Network.loadingFinished", lambda event: receive(event["requestId"], event.get("encodedDataLength", 0), final=True))
            cdp.on("Network.loadingFailed", lambda event: seen.pop(event["requestId"], None))
            cdp.on("Network.requestWillBeSent", lambda event: downloads.request(page, event) if downloads is not None else None)
            await cdp.send("Network.enable")
        if page not in tracking:
            tracking[page] = asyncio.create_task(attach())
        await tracking[page]
    async def finish_tracking():
        nonlocal tracking_active
        tracking_active = False
        for page, listener in request_listeners:
            page.remove_listener('request', listener)
        # Settle owned background operations before closing their targets.
        for task in tracking.values():
            if not task.done():
                task.cancel()
        if tracking:
            await asyncio.gather(*tracking.values(), return_exceptions=True)
        for cdp in cdp_sessions:
            try:
                await cdp.detach()
            except Exception:
                pass  # An owned page may already be closed by its resource callback.
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
    context.resources.push_async_callback(finish_tracking)
    if manage_budget:
        async def route_request(route):
            if modes.blocks(route.request):
                mode_receipt['blocked_requests'] += 1
                await route.abort()
                return
            if (context.params.get("block_media", True) if block_media is None else block_media) and route.request.resource_type in {"media", "font"}:
                await route.abort()
                return
            try:
                await budget.before_browser_request(route.request)
            except (BudgetExceeded, JobControl):
                await route.abort()
            else:
                await route.continue_()
        if not borrowed:
            await browser_context.route("**/*", route_request)
            if bridge is not None:
                async def block_websocket(socket):
                    budget.reason = 'unsupported_websocket_transport'
                    await socket.close()
                await browser_context.route_web_socket('**/*', block_websocket)
    managed_context = ManagedBrowserContext(browser_context, watch_page) if manage_budget or borrowed else browser_context
    if on_browser_created:
        await on_browser_created(browser, context=managed_context)
    page = selected_page or await managed_context.new_page()
    if selected_page and manage_budget:
        await watch_page(page)
    if accept_downloads:
        from .browser_downloads import BrowserDownloads
        downloads = BrowserDownloads(context, browser, browser_context, budget,
            owned_default_context=profile_path is not None)
        context.resources.push_async_callback(downloads.close)
        await downloads.start(page)
    session = BrowserSession(browser, managed_context, page, budget, page_owned=selected_page is None, downloads=downloads,
        engine=engine, provider=provider, adapter=adapter, network_bridge=bridge, browser_modes=mode_receipt)
    cache[key] = session
    context._browser_sessions = cache
    return session
