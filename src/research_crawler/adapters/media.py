"""Register the shared media manager with the single job runner."""
import asyncio

from ..core.sessions import controlled
from ..media import CAPABILITIES, download


def register(runner):
    slot = asyncio.Semaphore(1)

    async def handler(context):
        async def scheduled():
            async with slot:
                return await download(context)

        return await controlled(context, scheduled())

    runner.register("media.download", handler)
