"""Independent discovery strategies within the unified job/session/storage core."""
import asyncio

from ..core.sessions import controlled
from ..core.exclusive import lease
from ..core.account_pool import pooled_identity
from ..platforms import discovery_bilibili, discovery_reddit, discovery_stackexchange, discovery_tieba, discovery_xiaohongshu

MODULES = {"bili": discovery_bilibili, "reddit": discovery_reddit,
           "stackexchange": discovery_stackexchange, "tieba": discovery_tieba, "xhs": discovery_xiaohongshu}
CAPABILITIES = {"handler": "platform.discover", "status": "bounded_first_slice",
    "platforms": {key: module.CAPABILITIES for key, module in MODULES.items()}}


async def collect(context, *, transport=None, interval=1.0):
    platform = context.params.get("platform", "bili")
    if platform not in MODULES:
        raise ValueError("Discovery currently supports bili, reddit, stackexchange, tieba and xhs")
    return await MODULES[platform].collect(context, transport=transport, interval=interval)


def register(runner):
    async def scheduled(context):
        platform = context.params.get("platform", "bili")
        if platform not in MODULES:
            raise ValueError("Unsupported discovery platform")
        await lease(context, 'platform-slots', platform)
        async with pooled_identity(context, platform) as receipt:
            result = await collect(context)
            if result.get('status') == 'partial':
                context._pool_outcome = 'partial'
            if receipt:
                result['account_pool'] = receipt
            return result
    async def handler(context):
        return await controlled(context, scheduled(context))
    runner.register("platform.discover", handler)
