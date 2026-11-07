"""Crawl4AI content-fit algorithm, with job-owned page restoration.

Derived from async_crawler_strategy.py (Apache-2.0), pinned 838869b.
The unusual target-height ratio is intentional source compatibility.
"""
from __future__ import annotations

import asyncio


async def wait_content_images(page, context, adapter, *, timeout=1, strict=False):
    """Source adjust-only wait is bounded and nonfatal; control errors propagate."""
    from .hooks import wait_for_javascript
    await page.wait_for_load_state('domcontentloaded')
    await asyncio.sleep(.1)
    try:
        await wait_for_javascript(page, 'Array.from(document.images).every(i => i.complete)',
                                  context, adapter=adapter, timeout=timeout)
    except TimeoutError:
        if strict:
            raise
        return {'status': 'timeout', 'timeout_seconds': timeout, 'continued': True}
    return {'status': 'complete', 'timeout_seconds': timeout,
            'meaning': 'HTMLImageElement.complete; includes failed image loads'}


class ContentViewport:
    def __init__(self, page, adapter):
        self.page, self.adapter = page, adapter
        self.original = dict(page.viewport_size) if page.viewport_size else None
        self.cdp = None
        self.changed = False
        self.receipt = {'status': 'pending', 'algorithm': 'crawl4ai-content-fit', 'restored': False}

    async def apply(self):
        if self.original is None:
            raise ValueError('Content viewport adjustment requires an owned fixed viewport')
        dimensions = await self.adapter.evaluate(self.page,
            '({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})')
        width, height = dimensions['width'], dimensions['height']
        target_width = self.original['width']
        if any(type(n) is not int or not 1 <= n <= 100000 for n in (width, height)):
            raise ValueError('Content viewport dimensions exceed the supported 1..100000 pixel bounds')
        target_height = int(target_width * width / height * .95)
        if not 1 <= target_height <= 16384:
            raise ValueError('Content-fit target height exceeds the supported 1..16384 pixel bounds')
        target = {'width': target_width, 'height': target_height}
        metrics = {'width': width, 'height': height, 'deviceScaleFactor': 1,
                   'mobile': False, 'scale': min(target_width / width, target_height / height)}
        self.cdp = await self.page.context.new_cdp_session(self.page)
        # Mark first: cancellation can arrive after the browser applied a command.
        self.changed = True
        await self.page.set_viewport_size(target)
        await self.cdp.send('Emulation.setDeviceMetricsOverride', metrics)
        actual = await self.adapter.evaluate(self.page,
            '({width:innerWidth,height:innerHeight,deviceScaleFactor:devicePixelRatio})')
        self.receipt.update(status='complete', original=self.original, content=dimensions,
                            target=target, metrics=metrics, observed=actual)
        return self.receipt

    async def _restore(self):
        try:
            if self.changed and not self.page.is_closed():
                try:
                    await self.cdp.send('Emulation.clearDeviceMetricsOverride')
                finally:
                    # Playwright restores its context DPR/mobile configuration here.
                    await self.page.set_viewport_size(self.original)
                self.receipt['restored'] = True
            elif self.changed:
                self.receipt['restored'] = 'page_closed'
        finally:
            if self.cdp:
                await self.cdp.detach()
                self.cdp = None

    async def restore(self):
        # Do not leave a reused page scaled if the outer crawl is cancelled.
        cleanup = asyncio.create_task(self._restore())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
