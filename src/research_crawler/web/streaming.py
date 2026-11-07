"""Completion-order publication through the shared atomic job store.

The frozen Crawl4AI async_dispatcher.run_urls_stream exposes completed tasks
before slower peers. Here task ownership and cancellation remain in collect_web;
this adapter selects publication order without creating another dispatcher.
"""
import asyncio


async def results_in_order(items, tasks, *, stream=False, refill=None):
    if not stream:
        for item, task in zip(items, tasks):
            yield item, task
        return
    ready = asyncio.Queue()
    callbacks = []
    pending = {}
    def register(item, task):
        pending[task] = item
        callback = lambda completed, entry=item: ready.put_nowait((entry, completed))
        callbacks.append((task, callback))
        task.add_done_callback(callback)
    for item, task in zip(items, tasks):
        register(item, task)
    try:
        while pending:
            item,task=await ready.get()
            pending.pop(task)
            yield item,task
            # Caller has committed the yielded source before advancing here.
            if refill:
                for item, task in await refill(list(pending.values())):
                    register(item,task)
    finally:
        for task, callback in callbacks:
            task.remove_done_callback(callback)
