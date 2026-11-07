"""Explicit external Chrome target selection without taking browser ownership.

Adapts Crawl4AI 838869b browser_manager.py CDP/target-selection behavior.
The upstream first-page fallback is intentionally replaced by exact Target IDs.
Crawl4AI Apache-2.0 and attribution remain in licenses/ and the product notice.
"""
import asyncio
import math
from urllib.parse import urlsplit
from .exclusive import lease


async def lease_target(context, target_id):
    """Cross-process target lease; process death releases the OS lock."""
    await lease(context, 'cdp-targets', target_id)


async def _target_info(page):
    session = await page.context.new_cdp_session(page)
    try:
        return (await session.send('Target.getTargetInfo'))['targetInfo']
    finally:
        await session.detach()


class SelectedCDPContext:
    """A native context selection, independent of Playwright's merged wrapper.

    connect_over_cdp can expose pre-existing incognito targets through its
    default BrowserContext wrapper. Creating through that wrapper would select
    the default native context instead. Only page-scoped operations are exposed.
    """
    def __init__(self, browser, root, native_id, owner, pages=()):
        self.browser, self._root, self.native_id = browser, root, native_id
        self._owner, self._pages = owner, list(pages)

    @property
    def pages(self):
        return [page for page in self._pages if not page.is_closed()]

    async def new_cdp_session(self, page):
        if page not in self._pages:
            raise ValueError('Page does not belong to the selected CDP scope')
        return await page.context.new_cdp_session(page)

    async def new_page(self):
        request = {'url':'about:blank'}
        if self.native_id:
            request['browserContextId'] = self.native_id
        creation = asyncio.create_task(self._root.send('Target.createTarget',request))
        async def close_owned_target():
            try:
                # Chrome can create a page before the protocol response reaches
                # us. Keep that response alive across caller cancellation so
                # cleanup can learn the ID, rather than orphan the native page.
                target = (await asyncio.wait_for(asyncio.shield(creation),10))['targetId']
                await self._root.send('Target.closeTarget',{'targetId':target})
                # CDP acknowledges the close request before target destruction.
                # Observe disappearance before detaching our protocol session.
                async with asyncio.timeout(3):
                    while any(item['targetId']==target for item in
                              (await self._root.send('Target.getTargets',{}))['targetInfos']):
                        await asyncio.sleep(.02)
            except Exception:
                pass  # Its owned page or the external browser may already close.
            finally:
                if not creation.done():
                    creation.cancel()
                    await asyncio.gather(creation,return_exceptions=True)
        self._owner.resources.push_async_callback(close_owned_target)
        try:
            target = (await asyncio.shield(creation))['targetId']
            async with asyncio.timeout(10):
                while True:
                    await self._owner.check_control()
                    for wrapper in self.browser.contexts:
                        for page in wrapper.pages:
                            info = await _target_info(page)
                            if info['targetId'] != target:
                                continue
                            if self.native_id and info.get('browserContextId','') != self.native_id:
                                raise ValueError('Created CDP page has a different native context')
                            self._pages.append(page)
                            return page
                    await asyncio.sleep(.02)
        except BaseException:
            await close_owned_target()
            raise


async def connect(playwright, params, context):
    endpoint = params.get('cdp_url')
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {'http','https','ws','wss'} or not parsed.hostname:
        raise ValueError('Choose an explicit Chrome CDP HTTP(S) or WS(S) endpoint')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('CDP endpoint must not contain credentials or query tokens')
    if params.get('browser_family','chromium') not in {'chrome','chromium'}:
        raise ValueError('This adapter controls explicitly selected Chrome/Chromium only')
    target_id, context_id = params.get('target_id'), params.get('browser_context_id')
    if target_id is not None and (not isinstance(target_id,str) or not target_id):
        raise ValueError('target_id must be a nonempty native target identifier')
    if context_id is not None and not isinstance(context_id,str):
        raise ValueError('browser_context_id must be a native context identifier')
    timeout = params.get('cdp_timeout_seconds',15)
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 1<=timeout<=60:
        raise ValueError('cdp_timeout_seconds must be1..60')
    browser = await playwright.chromium.connect_over_cdp(endpoint,timeout=timeout*1000)
    if not target_id and context_id is None:
        return browser, None, None
    root = await browser.new_browser_cdp_session()
    async def detach():
        try:
            await root.detach()
        except Exception:
            pass
    context.resources.push_async_callback(detach)
    matches=[]
    selected=None
    native_id=context_id
    for wrapper in browser.contexts:
        for page in wrapper.pages:
            info = await _target_info(page)
            if target_id and info['targetId']==target_id and (context_id is None or info.get('browserContextId','')==context_id):
                selected=page;native_id=info.get('browserContextId','')
            if context_id is not None and info.get('browserContextId','')==context_id:
                matches.append(page)
    if target_id and selected is None:
        raise ValueError('Selected CDP target/context is unavailable; no first-tab fallback')
    if selected is not None and selected not in matches:
        matches.append(selected)
    # An empty native context has no Page to inspect. Target.createTarget below
    # addresses the explicit ID directly; Chrome rejects a nonexistent context.
    return browser, SelectedCDPContext(browser,root,native_id,context,matches), selected
