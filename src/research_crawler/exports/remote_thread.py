"""Isolate Windows asyncmy's Selector requirement from the application's loop.

The browser/main process keeps Proactor for subprocess support. Each affected
MySQL writer owns one thread, one Selector loop and its database connection.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import threading
from uuid import uuid4


async def _settle(future):
    """Finish cleanup even if more than one outer cancellation arrives."""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


class _SelectorThread:
    def __init__(self):
        self.ready = Future()
        self.thread = threading.Thread(target=self._main,
            name="research-crawler-mysql-" + uuid4().hex[:12], daemon=True)
        self.thread.start()

    def _main(self):
        loop = None
        try:
            loop = asyncio.SelectorEventLoop()
            asyncio.set_event_loop(loop)
            self.ready.set_result(loop)
            loop.run_forever()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            else:
                raise
        finally:
            if loop is not None:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
                loop.close()

    async def run(self, factory):
        loop = await asyncio.shield(asyncio.wrap_future(self.ready))
        completion = Future()
        cancellation = threading.Event()
        holder = []

        def finish(task):
            try:
                completion.set_result(task.result())
            except BaseException as error:
                completion.set_exception(error)

        def start():
            async def invoke():
                return await factory()
            task = loop.create_task(invoke())
            holder.append(task)
            task.add_done_callback(finish)
            if cancellation.is_set():
                task.cancel()

        def cancel():
            cancellation.set()
            if holder:
                holder[0].cancel()

        loop.call_soon_threadsafe(start)
        waiting = asyncio.wrap_future(completion)
        try:
            return await asyncio.shield(waiting)
        except asyncio.CancelledError:
            # Do not cancel the cross-thread completion Future: its completion
            # must mean the actual thread Task's transaction/finally settled.
            if not completion.done():
                loop.call_soon_threadsafe(cancel)
            try:
                await _settle(waiting)
            except BaseException:
                pass
            raise

    async def stop(self):
        try:
            loop = await asyncio.shield(asyncio.wrap_future(self.ready))
            loop.call_soon_threadsafe(loop.stop)
        finally:
            await asyncio.to_thread(self.thread.join)


class SelectorSQLWriter:
    """Async proxy; all driver/engine work stays on its owning Selector loop."""
    def __init__(self):
        self.owner = _SelectorThread()
        self._writer = None
        self._closing = None

    @classmethod
    async def open(cls, settings, native_open):
        proxy = cls()
        async def opening():
            # Keep ownership inside the thread operation before signalling
            # completion, even if the caller is cancelled on that boundary.
            proxy._writer = await native_open(settings)
        try:
            await proxy.owner.run(opening)
            return proxy
        except BaseException:
            await proxy.close()
            raise

    async def write_batch(self, batch, manifest, export_job_id, *, complete=False):
        if self._closing is not None:
            raise RuntimeError("Remote writer is closing")
        return await self.owner.run(lambda: self._writer.write_batch(
            batch, manifest, export_job_id, complete=complete))

    async def _close(self):
        async def dispose():
            current = asyncio.current_task()
            active = [task for task in asyncio.all_tasks() if task is not current]
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            if self._writer is not None:
                await self._writer.close()
        try:
            await self.owner.run(dispose)
        finally:
            await self.owner.stop()

    async def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            try:
                await _settle(self._closing)
            finally:
                raise
