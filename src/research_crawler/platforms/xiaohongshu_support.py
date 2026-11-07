"""Job-scoped primitives for the three source-derived social adapters.

Request/parser knowledge: MediaCrawler 54a3d92412f4b20c06b45f1d542b78dd92049d6d,
Copyright (c) 2025 relakkes@gmail.com, NON-COMMERCIAL LEARNING LICENSE 1.1.
Changes: pure normalization, injected resources, explicit budgets, atomic resume.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from ..core.jobs import NeedsLogin
from .privacy import anonymize_user_id as anonymous, mask_nickname as masked

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


class PlatformError(RuntimeError):
    def __init__(self, platform, code):
        self.platform, self.code = platform, code
        super().__init__(f"{platform}: {code}")


def timestamp(value, *, milliseconds=False):
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(float(value) / (1000 if milliseconds else 1), timezone.utc).isoformat()


def require_id(value, platform):
    if value is None or not str(value).strip():
        raise PlatformError(platform, "missing_record_id")
    return str(value)


def limits(params):
    return {name: min(100 if name == "max_pages" else 1000, max(0, int(params.get(name, default))))
            for name, default in (("max_posts", 20), ("max_comments", 20), ("max_replies", 20), ("max_pages", 5))}


async def keyword_batch(context, collector, *, transport, interval):
    """Expand selected search keywords with one shared job/session/request budget.

    Content caps are shared across keywords. Unvisited keywords are explicitly
    partial, so exhausting a budget never looks like searching every keyword.
    """
    original = context.params
    keywords = original.get("keywords")
    if original.get("mode", "detail") != "search" or keywords is None:
        return None
    if isinstance(keywords, str): keywords = keywords.split(",")
    if not isinstance(keywords, list): raise ValueError("keywords must be a list or comma-separated string")
    keywords = list(dict.fromkeys(str(value).strip() for value in keywords if str(value).strip()))
    if not keywords: raise ValueError("At least one keyword is required")
    remaining, results = limits(original)["max_posts"], []
    try:
        for keyword in keywords:
            if remaining <= 0:
                results.append({"keyword": keyword, "status": "partial", "reason": "task_post_limit", "streams": []})
                continue
            context.params = dict(original, keyword=keyword, keywords=None, max_posts=remaining)
            result = await collector(context, transport=transport, interval=interval)
            result["keyword"] = keyword
            results.append(result)
            remaining -= sum(item["count"] for item in result.get("streams", []))
    finally:
        context.params = original
    return {"status": "complete" if all(item["status"] == "complete" for item in results) else "partial",
            "platform": results[0].get("platform", original.get("platform")), "keywords": results,
            "evidence": "injected_transport" if transport is not None else "platform_http",
            "media_downloaded": False}


def selected_values(params, mode):
    values = params.get("creators" if mode == "creator" else "ids", [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not values:
        raise ValueError("Supply creators for creator mode or ids for detail mode")
    return values


@dataclass
class Page:
    items: list
    cursor: object
    more: bool


def page(data, item_key, cursor_key="cursor", more_key="has_more", *, cursor=None):
    if not isinstance(data.get(item_key), list):
        raise PlatformError("platform", "invalid_items")
    more = data.get(more_key)
    if more not in (True, False, 0, 1) or more_key not in data:
        raise PlatformError("platform", "missing_pagination_flag")
    if bool(more) and cursor_key not in data and cursor is None:
        raise PlatformError("platform", "missing_cursor")
    return Page(data[item_key], data.get(cursor_key) if cursor is None else cursor, bool(more))


async def stream(context, name, fetch, normalize, *, initial="", maximum=20, page_limit=5, child=None):
    """Keep the current page cursor on a mid-page cap or incomplete child stream.

    Children commit independently before the parent checkpoint. Replaying a parent
    page after interruption is safe; seen IDs are durable and child streams resume.
    """
    cp = await context.checkpoint(name)
    metadata = (cp or {}).get("metadata", {})
    seen = set(metadata.get("seen_ids", []))
    cursors = set(metadata.get("seen_cursors", []))
    if cp and cp["exhausted"]:
        return {"stream": name, "complete": True, "count": len(seen), "reason": "end"}
    cursor = cp["cursor"] if cp else initial
    pending = bool(metadata.get("child_pending"))
    pages = 0
    reason = "record_limit" if len(seen) >= maximum else "page_limit"
    while pages < page_limit and (len(seen) < maximum or pending):
        await context.check_control()
        result = await fetch(cursor)
        if not isinstance(result, Page) or not isinstance(result.items, list):
            raise PlatformError("platform", "invalid_page")
        records, truncated, pending = [], False, False
        for item in result.items:
            if not isinstance(item, dict):
                raise PlatformError("platform", "invalid_item")
            record = normalize(item)
            if inspect.isawaitable(record):
                record = await record
            identity = record.native_id
            if identity not in seen and len(seen) >= maximum:
                truncated = True
                break
            if child is not None:
                child_result = await child(item, record)
                pending = pending or not child_result.get("complete", False)
            if identity not in seen:
                records.append(record)
                seen.add(identity)
        old = json.dumps(cursor, sort_keys=True)
        nxt = json.dumps(result.cursor, sort_keys=True)
        exhausted = not result.more and not truncated and not pending
        stalled = result.more and (old == nxt or nxt in cursors)
        reason = "end" if exhausted else "child_limit" if pending else "record_limit" if truncated else "repeated_cursor" if stalled else "next_page"
        if not pending and not truncated:
            cursors.add(old)
        retain = truncated or pending or stalled
        cp = await context.commit_page(name, records, next_cursor=cursor if retain else result.cursor,
            exhausted=exhausted, expected_revision=cp["revision"] if cp else 0,
            metadata={"seen_ids": sorted(seen), "seen_cursors": sorted(cursors), "reason": reason, "child_pending": pending})
        pages += 1
        if exhausted or retain:
            return {"stream": name, "complete": exhausted, "count": len(seen), "reason": reason}
        cursor = result.cursor
        reason = "record_limit" if len(seen) >= maximum else "page_limit"
    return {"stream": name, "complete": False, "count": len(seen), "reason": reason}


class Client:
    def __init__(self, http, context, platform, *, interval=1):
        self.http, self.context, self.platform = http, context, platform
        self.account = context.params.get("account") or "anonymous"
        self.interval, self.last_request = max(0, interval), 0.0

    def cookies(self, url):
        # Let httpx select cookies using the actual URL's domain/path rules.
        request = self.http.build_request("GET", url)
        return request.headers.get("cookie", "")

    async def request(self, method, url, **kwargs):
        await self.context.check_control()
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        await self.context.check_control()
        self.last_request = time.monotonic()
        response = await self.http.request(method, url, **kwargs)
        if response.status_code in {401, 403, 461, 471}:
            raise NeedsLogin(self.platform, self.account, "Selected account requires login or a user-completed platform challenge")
        if response.status_code != 200:
            raise PlatformError(self.platform, f"http_{response.status_code}")
        return response

    async def json(self, method, url, **kwargs):
        response = await self.request(method, url, **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise PlatformError(self.platform, "invalid_json") from None
        if not isinstance(data, dict):
            raise PlatformError(self.platform, "invalid_envelope")
        return data

    async def resolve(self, value, allowed):
        # Restrict every redirect, including scheme, before making a request.
        for _ in range(5):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password:
                raise ValueError("Only official platform URLs are accepted")
            await self.context.check_control()
            response = await self.http.get(value, follow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                value = urljoin(value, response.headers.get("location", ""))
                continue
            if response.status_code in {401, 403}:
                raise NeedsLogin(self.platform, self.account)
            if response.status_code != 200:
                raise PlatformError(self.platform, f"http_{response.status_code}")
            return value
        raise PlatformError(self.platform, "redirect_limit")


def summary(platform, streams):
    return {"status": "complete" if all(item["complete"] for item in streams) else "partial",
            "platform": platform, "streams": streams, "evidence": "platform_http",
            "creator_profiles": "upstream_intentionally_not_persisted", "media_downloaded": False}
