"""Owned browser generations; source: Crawl4AI browser_manager.py, Apache-2.0.

The frozen source rotates after max_pages_before_recycle and drains references.
Here an open page (including a retained session) pins its original generation.
"""
from __future__ import annotations

import asyncio


def recycle_limit(params):
    value = params.get('max_pages_before_recycle', 0)
    if type(value) is not int or not 0 <= value <= 100000:
        raise ValueError('max_pages_before_recycle must be an integer in 0..100000')
    if value and (params.get('cdp_url') or params.get('browser_profile')):
        raise ValueError('Browser recycling requires an owned ephemeral context; CDP and named profiles cannot rotate')
    return value


class BrowserGenerations:
    def __init__(self, context, limit, factory):
        self.owner, self.limit, self.factory = context, limit, factory
        self.generations = []
        self.current = None
        self.context = self
        self.lock = asyncio.Lock()
        self.cleanup_tasks = set()
        self.closing = False
        self.receipt = {'limit': limit, 'launches': 0, 'retired': 0, 'closed': 0,
                        'scope': 'new_pages_after_bootstrap', 'max_draining': 3}

    def __getattr__(self, name):
        session = self.current['session']
        return getattr(session, name) if hasattr(session, name) else getattr(session.context, name)

    async def start(self):
        await self._launch()
        self.page = self.current['session'].page
        return self

    async def _launch(self):
        session, stack = await self.factory()
        generation = {'session': session, 'stack': stack, 'served': 0, 'retired': False,
                      'closed': False}
        self.generations.append(generation)
        self.current = generation
        self.receipt['launches'] += 1
        # Subsequent bootstrap pages are not handed to a caller.
        if len(self.generations) > 1:
            await session.page.close()

    async def _drain(self):
        for generation in self.generations:
            if generation['retired'] and not generation['closed'] and not generation['session'].context.pages:
                await generation['stack'].aclose()
                generation['closed'] = True
                self.receipt['closed'] += 1

    async def new_page(self):
        async with self.lock:
            await self.owner.check_control()
            await self._drain()
            if self.current['served'] >= self.limit:
                # Bound accumulated generations without interrupting pinned pages.
                while sum(g['retired'] and not g['closed'] for g in self.generations) >= 3:
                    await self.owner.check_control()
                    await asyncio.sleep(.05)
                    await self._drain()
                if not self.current['retired']:
                    self.current['retired'] = True
                    self.receipt['retired'] += 1
                await self._launch()
                await self._drain()
            page = await self.current['session'].context.new_page()
            self.current['served'] += 1
            page.once('close', lambda _: self._schedule_drain())
            return page

    def _schedule_drain(self):
        if not self.closing:
            task = asyncio.create_task(self.drain())
            self.cleanup_tasks.add(task)
            # Retain tasks until owner cleanup so exceptions are observed.

    def session_for(self, page):
        for generation in self.generations:
            if page.context == getattr(generation['session'].context, '_context', generation['session'].context):
                return generation['session']
        raise ValueError('Page does not belong to this browser pool')

    async def new_cdp_session(self, page):
        return await page.context.new_cdp_session(page)

    async def drain(self):
        async with self.lock:
            await self._drain()

    async def _close(self):
        # Each stack owns only this job/configuration's generation.
        self.closing = True
        outcomes = await asyncio.gather(*self.cleanup_tasks, return_exceptions=True)
        for generation in reversed(self.generations):
            if not generation['closed']:
                try:
                    await generation['stack'].aclose()
                except Exception as error:
                    outcomes.append(error)
                finally:
                    generation['closed'] = True
                    self.receipt['closed'] += 1
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome

    async def close(self):
        cleanup = asyncio.create_task(self._close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
