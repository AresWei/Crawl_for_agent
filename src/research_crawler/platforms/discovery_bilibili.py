"""Independent public popular-list adapter, verified against Bilibili's own response.

The legacy hot/feed mode is popular videos, not a personalized feed. Distinct
trending/homefeed modes delegate to the source-verified homepage adapter.
Uses existing MediaCrawler-derived normalizer under licenses/mediacrawler/LICENSE.
"""
from __future__ import annotations

from dataclasses import replace

from ..core.jobs import NeedsLogin
from ..core.sessions import open_http
from .bilibili import normalize_video
from .discovery_common import DiscoveryClient, DiscoveryUnavailable, schema_error, unavailable_summary
from .weibo._shared import budgets, walk

ENDPOINT = "https://api.bilibili.com/x/web-interface/popular"
CAPABILITIES = {"modes": ["hot", "feed", "trending", "homefeed"], "feed_kind": "mode_specific",
    "mode_kinds": {"hot": "public_popular", "feed": "public_popular_alias",
                   "trending": "search_trending_topics", "homefeed": "homepage_recommendations"},
    "pagination": "popular: pn/ps + no_more; homefeed: bounded snapshots; trending: one snapshot",
    "personalized": "homefeed depends on selected session; hot/trending are public",
    "comments": False, "media": ["cover_url"], "live_verified": "anonymous_first_page_2026-11-07",
    "limits": ["public undocumented web endpoint; access may change", "feed is an alias of hot"]}


async def collect(context, *, transport=None, interval=1.0):
    params = context.params
    mode = params.get("mode", "hot")
    if mode in ('trending', 'homefeed'):
        from .discovery_bili_home import collect as collect_home
        return await collect_home(context, transport=transport, interval=interval)
    if mode not in CAPABILITIES["modes"]:
        raise ValueError("Bilibili discovery modes: hot, feed, trending, homefeed")
    limits = budgets(params)
    size = int(params.get("page_size", 20))
    if not 1 <= size <= 50:
        raise ValueError("page_size must be 1..50")
    http = await open_http(context, platform="bili", account=params.get("account"),
        transport=transport, headers={"User-Agent": "ResearchCrawler/0.1 (local public research)",
                                     "Referer": "https://www.bilibili.com/v/popular/all"})
    client = DiscoveryClient(http, context, "bili", interval=interval)

    async def fetch(page):
        body, evidence = await client.get_json(ENDPOINT, {"pn": page, "ps": size})
        if not isinstance(body, dict):
            raise schema_error(evidence)
        if body.get("code") in (-101, -111):
            raise NeedsLogin("bili", params.get("account") or "anonymous")
        if body.get("code") != 0:
            raise DiscoveryUnavailable("blocked" if body.get("code") in (-352, -412) else "api_error",
                                       dict(evidence, api_code=body.get("code")))
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list) or type(data.get("no_more")) is not bool:
            raise schema_error(evidence)
        rows = []
        for rank, item in enumerate(data["list"], (page - 1) * size + 1):
            if not isinstance(item, dict) or not item.get("aid"):
                raise schema_error(evidence)
            record = replace(normalize_video({"View": item}), source_date_field="data.list[].pubdate")
            record.coverage.update(endpoint="/x/web-interface/popular", evidence=evidence,
                feed_kind="public_popular", rank_at_observation=rank, comments_collected=False)
            # The feed exposes description; preserve it without the old detail
            # normalizer's 500-character display cap.
            record.payload["desc"] = str(item.get("desc", ""))
            record.coverage.update(description_truncated=False, description_limit=None)
            rows.append(record)
        return rows, page + 1, data["no_more"]

    try:
        result = await walk(context, f"discovery:bili:popular:{size}", fetch, **limits)
    except DiscoveryUnavailable as error:
        return await unavailable_summary(context, "bili", mode, error)
    return {"status": "complete" if result["complete"] else "partial", "platform": "bili",
        "mode": mode, "feed_kind": "public_popular", "coverage": [result],
        "coverage_complete": result["complete"], "scope": "observed_public_popular_listing"}
