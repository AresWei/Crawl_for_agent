import asyncio
from ..analysis.wordcloud_job import generate
from ..core.sessions import controlled

CAPABILITIES={'implemented':['comment_word_frequencies','source_linked_wordcloud'],'paid_fallback':False}

def register(runner):
    slot=asyncio.Semaphore(1)
    async def handler(context):
        async def scheduled():
            async with slot:
                return await generate(context)
        return await controlled(context,scheduled())
    runner.register('comments.wordcloud',handler)
