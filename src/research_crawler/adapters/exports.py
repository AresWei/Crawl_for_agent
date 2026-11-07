"""Register exports on the shared job runner; no second storage process."""
import asyncio

from ..exports import CAPABILITIES, export_records
from ..exports.remote_jobs import export_remote
from ..exports.remote import CAPABILITIES as REMOTE_CAPABILITIES

CAPABILITIES = {**CAPABILITIES, 'remote': REMOTE_CAPABILITIES}


def register(runner):
    slot = asyncio.Semaphore(1)

    async def handler(context):
        async with slot:
            return await export_records(context)

    runner.register("export.records", handler)
    runner.register("export.remote", export_remote)
