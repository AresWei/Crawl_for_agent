"""Single platform dispatcher; no upstream programs or second storage backend."""
from __future__ import annotations

import asyncio
import math

from ..core.accounts import AccountStore
from ..core.account_pool import pooled_identity
from ..core.jobs import JobContext, JobRunner, NeedsLogin
from ..core.sessions import controlled
from ..core.exclusive import lease
from ..platforms import PLATFORMS, capability_registry, module_for

CAPABILITIES = capability_registry()


def scoped_cookies(context: JobContext, account: str | None):
    return AccountStore(context.paths).cookies("bili", account)


async def collect(context: JobContext, *, transport=None, interval: float | None = None) -> dict:
    params = context.params
    interval = params.get("interval", 1.0 if interval is None else interval)
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 <= interval <= 60:
        raise ValueError("interval must be a finite number from 0 to 60 seconds")
    if type(params.get("verify_ssl", True)) is not bool:
        raise ValueError("verify_ssl must be a boolean")
    platform = params.get("platform", "bili")
    module = module_for(platform)
    if params.get("mode", "detail") not in module.CAPABILITIES.get("modes", []):
        raise ValueError("This mode is not supported by the selected platform")
    maximum = params.get("max_items", params.get("max_posts", 20))
    params.setdefault("max_items", maximum)
    params.setdefault("max_posts", maximum)
    if "query" in params and "keywords" not in params:
        params["keywords"] = [params["query"]]
    if "keyword" in params and "keywords" not in params:
        params["keywords"] = [params["keyword"]]
    if "keywords" in params and "keyword" not in params:
        params["keyword"] = params["keywords"][0] if isinstance(params["keywords"], list) else params["keywords"]
    if "urls" in params and "ids" not in params:
        params["ids"] = params["urls"]
    return await module.collect(context, transport=transport, interval=interval)


def register(runner: JobRunner) -> None:
    async def scheduled(context: JobContext) -> dict:
        platform = context.params.get("platform", "bili")
        if platform not in PLATFORMS:
            raise ValueError("Unknown platform")
        await lease(context, 'platform-slots', platform)
        async with pooled_identity(context, platform) as pool_receipt:
            result = await selected(context, platform)
            if pool_receipt:
                result["account_pool"] = pool_receipt
                if result.get("status") == "partial" or result.get("coverage_complete") is False:
                    context._pool_outcome = "partial"
            return result

    async def selected(context, platform):
        account=context.params.get('account');accounts=AccountStore(context.paths);revision=None
        if account:
            try:revision=await asyncio.to_thread(accounts.validation_revision,platform,account)
            except KeyError:pass
        try:
            return await collect(context)
        except NeedsLogin as error:
            if account and revision is not None and error.platform==platform and error.account==account:
                await asyncio.to_thread(accounts.record_validation,platform,account,
                    {'state':'auth_required','reason_code':'platform_request_requires_authentication','method':'collection_response'},
                    expected_revision=revision)
            raise

    async def handler(context: JobContext) -> dict:
        return await controlled(context, scheduled(context))

    runner.register("platform.collect", handler)
