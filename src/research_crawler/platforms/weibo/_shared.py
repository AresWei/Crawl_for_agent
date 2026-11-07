"""Small shared adapters for the source-migrated social clients.

Privacy helpers derive from MediaCrawler tools/user_hash.py (Copyright 2025
relakkes@gmail.com, NON-COMMERCIAL LEARNING LICENSE 1.1). Other code integrates
the platform page algorithms with the application's atomic checkpoint lifecycle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx
from bs4 import BeautifulSoup

from ...core.jobs import NeedsLogin
from ..privacy import anonymize_user_id, mask_nickname
from ...core.records import Record

logger = logging.getLogger(__name__)


class APIError(RuntimeError):
    def __init__(self, platform, code):
        self.platform, self.code = platform, code
        super().__init__(f"{platform}: {code}")


def extract_text_from_html(value):
    return BeautifulSoup(value or "", "html.parser").get_text(separator=" ", strip=True)


def get_time_str_from_unix_time(value):
    stamp = int(value)
    if stamp > 1000000000000:
        stamp /= 1000
    return datetime.fromtimestamp(stamp, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def timestamp_date(value):
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat() if value else None


def budgets(params):
    result = {"maximum": int(params.get("max_items", params.get("max_records", 20))),
              "page_limit": int(params.get("max_pages", 20))}
    if not 1 <= result["maximum"] <= 1000 or not 1 <= result["page_limit"] <= 100:
        raise ValueError("max_items must be 1..1000 and max_pages 1..100")
    for field in ("max_comments", "max_replies"):
        if not 0 <= int(params.get(field, 0)) <= 1000:
            raise ValueError(f"{field} must be 0..1000")
    return result


def selections(params, mode):
    values = params.get("creators", params.get("ids", params.get("urls", []))) if mode == "creator" else params.get("ids", params.get("urls", []))
    if not isinstance(values, list) or not values or len(values) > 50:
        raise ValueError("Select 1..50 IDs or URLs")
    return list(dict.fromkeys(str(v).strip() for v in values))


def keywords(params):
    values = params.get("keywords", params.get("query", ""))
    values = values.split(",") if isinstance(values, str) else values
    if not isinstance(values, list) or not values or len(values) > 20 or not all(str(v).strip() for v in values):
        raise ValueError("Supply 1..20 keywords")
    return list(dict.fromkeys(str(v).strip() for v in values))


class Client:
    def __init__(self, http, context, platform, *, interval=1.0):
        self.http, self.context, self.platform = http, context, platform
        self.account = context.params.get("account") or "anonymous"
        self.interval, self.last_request = interval, 0.0

    async def throttle(self):
        await self.context.check_control()
        wait = self.interval - (time.monotonic() - self.last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        await self.context.check_control()
        self.last_request = time.monotonic()

    async def request(self, method, url, **kwargs):
        await self.throttle()
        response = await self.http.request(method, url, **kwargs)
        if response.status_code in (401, 403) or (response.is_redirect and any(s in response.headers.get("location", "") for s in ("login", "passport", "signin"))):
            raise NeedsLogin(self.platform, self.account)
        if response.status_code != 200:
            raise APIError(self.platform, response.status_code)
        return response

    def json(self, response):
        try:
            data = response.json()
        except ValueError:
            raise APIError(self.platform, "invalid_json") from None
        if not isinstance(data, dict):
            raise APIError(self.platform, "invalid_envelope")
        return data


async def walk(context, stream, fetch, *, initial=1, maximum=20, page_limit=20):
    """Commit normalized pages and cursor together; replay a capped page safely.

    fetch(cursor) returns (records, next_cursor, exhausted). The source-specific
    fetch validates its own pagination envelope before any result is committed.
    """
    cp = await context.checkpoint(stream)
    meta = (cp or {}).get("metadata", {})
    seen, cursors = set(meta.get("seen_ids", [])), set(meta.get("seen_cursors", []))
    if cp and cp["exhausted"]:
        return {"complete": True, "count": len(seen), "reason": "end"}
    cursor = cp["cursor"] if cp else initial
    for _ in range(page_limit):
        if len(seen) >= maximum:
            return {"complete": False, "count": len(seen), "reason": "record_limit"}
        fetch.remaining = maximum - len(seen)
        fetch.seen_record_keys = seen
        page = await fetch(cursor)
        records, next_cursor, ended = page[:3]
        if type(ended) is not bool:
            raise ValueError("Page end marker must be explicit")
        batch, truncated = [], bool(page[3]) if len(page) > 3 else False
        for record in records:
            if record.key in seen:
                continue
            if len(seen) >= maximum:
                truncated = True
                break
            batch.append(record)
            seen.add(record.key)
        marker = json.dumps(cursor, sort_keys=True)
        next_marker = json.dumps(next_cursor, sort_keys=True)
        stalled = not ended and not truncated and (next_marker == marker or next_marker in cursors)
        cursors.add(marker)
        exhausted = ended and not truncated
        reason = "end" if exhausted else "record_limit" if truncated else "repeated_cursor" if stalled else "next_page"
        cp = await context.commit_page(stream, batch, next_cursor=cursor if truncated or stalled else next_cursor,
            exhausted=exhausted, expected_revision=cp["revision"] if cp else 0,
            metadata={"seen_ids": sorted(seen), "seen_cursors": sorted(cursors), "reason": reason})
        if exhausted or truncated or stalled:
            return {"complete": exhausted, "count": len(seen), "reason": reason}
        cursor = next_cursor
    return {"complete": False, "count": len(seen), "reason": "page_limit"}


def bounded_items(fetch, items, platform, kind, identity):
    """Apply the output budget before potentially expensive detail hydration."""
    seen = getattr(fetch, "seen_record_keys", set())
    pending = [item for item in items if not identity(item) or Record(platform, kind, str(identity(item)), "", {}).key not in seen]
    remaining = getattr(fetch, "remaining", len(pending))
    return pending[:remaining], len(pending) > remaining


async def observations(context, platform, kinds):
    result, offset = {}, 0
    while True:
        rows = await asyncio.to_thread(context.store.observations, context.job_id, limit=1000, offset=offset)
        for row in rows:
            if row["source"] == platform and row["kind"] in kinds:
                result[row["record_key"]] = row
        if len(rows) < 1000:
            return list(result.values())
        offset += len(rows)


def summary(platform, mode, coverage, **extra):
    return {"status": "complete" if all(c.get("complete", False) for c in coverage) else "partial",
            "platform": platform, "mode": mode, "coverage": coverage,
            "creator_profiles_persisted": False, **extra}
