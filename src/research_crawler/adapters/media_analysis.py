"""Register CPU media analysis in the shared job lifecycle."""
import asyncio
from ..core.sessions import controlled
from ..intelligence.ocr_asr import analyze


def register(runner):
    slot = asyncio.Semaphore(1)

    def handler(operation):
        async def run(context):
            async def owned_slot():
                async with slot:
                    return await analyze(context, operation)
            return await controlled(context, owned_slot())
        return run

    runner.register("media.ocr", handler("ocr"))
    runner.register("media.transcribe", handler("asr"))
