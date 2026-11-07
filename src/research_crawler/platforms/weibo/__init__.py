# Copyright (c) 2025 relakkes@gmail.com
# Adapted from MediaCrawler media_platform/weibo/{client,core,help,media}.py and
# store/weibo/__init__.py at 54a3d92412f4b20c06b45f1d542b78dd92049d6d.
# NON-COMMERCIAL LEARNING LICENSE 1.1; licenses/mediacrawler/LICENSE.
# Changes: scoped shared HTTP, pure privacy-preserving output, atomic page resume.
from __future__ import annotations

import json
import re
from datetime import timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urlsplit

from ...core.jobs import NeedsLogin
from ...core.records import Record
from ._shared import (APIError, Client, anonymize_user_id, mask_nickname, budgets,
                      selections, keywords, observations, walk, summary, bounded_items)

CAPABILITIES = {"implemented": True, "status": "source_migrated",
    "modes": ["search", "detail", "creator"], "comments": "hotflow_cursor",
    "replies": "embedded_only", "media": ["images", "cover", "video", "repost_media"],
    "creator_profiles": False, "live_verified": False,
    "limitations": ["Replies are embedded samples; upstream has no exhaustive reply endpoint.",
                    "Long-text failure retains original text with incomplete coverage."]}


def note_id(value):
    if re.fullmatch(r"\d+", value):
        return value
    url = urlsplit(value)
    if url.hostname in {"m.weibo.cn", "weibo.com", "www.weibo.com"}:
        match = re.fullmatch(r"/(?:detail|status|\d+)/(\d+)/?", url.path)
        if match:
            return match[1]
    raise ValueError("Expected numeric Weibo post ID or official detail URL")


def creator_id(value):
    if value.isdigit():
        return value
    url = urlsplit(value)
    if url.hostname in {"m.weibo.cn", "weibo.com", "www.weibo.com"}:
        match = re.fullmatch(r"/(?:u/)?(\d+)/?", url.path)
        if match:
            return match[1]
    raise ValueError("Expected numeric Weibo creator ID or official creator URL")


def cards(data):
    result = []
    for item in data:
        if item.get("card_type") == 9 and isinstance(item.get("mblog"), dict):
            result.append(item["mblog"])
        for child in item.get("card_group") or []:
            if child.get("card_type") == 9 and isinstance(child.get("mblog"), dict):
                result.append(child["mblog"])
    return result


def media_candidates(mblog):
    source = mblog if mblog.get("pics") or mblog.get("page_info") else (mblog.get("retweeted_status") or mblog)
    info = source.get("page_info") or {}
    video = []
    for container in (info.get("media_info") or {}, info.get("urls") or {}):
        for key in ("mp4_1080p_mp4", "mp4_hd_mp4", "mp4_720p_mp4", "hevc_mp4_hd", "mp4_ld_mp4", "stream_url"):
            url = container.get(key)
            if isinstance(url, str) and url and url not in video:
                video.append(url)
    if video:
        items = [{"kind": "video", "url": video[0], "backup_urls": video[1:]}]
        cover = (info.get("page_pic") or {}).get("url")
        return ([{"kind": "image", "url": cover, "role": "cover"}] if cover else []) + items
    result = []
    for pic in source.get("pics") or []:
        raw = pic if isinstance(pic, str) else pic.get("url", "") if isinstance(pic, dict) else ""
        if not raw:
            continue
        parts = urlsplit(raw)
        segments = [part for part in parts.path.split("/") if part]
        if segments:
            segments[0] = "large"
        large = f"{parts.scheme}://{parts.netloc}/{'/'.join(segments)}" if parts.netloc else raw
        # Preserve the upstream proxy candidate without silently sending media to it.
        result.append({"kind": "image", "url": large, "upstream_proxy_candidate": "https://i1.wp.com/" + parts.netloc + "/" + "/".join(segments)})
    return result


def date_fields(value):
    if not value:
        return None, None, None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(dt.timestamp()), str(dt.astimezone(timezone(timedelta(hours=8)))), dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None, None, None


def normalize_note(item, *, keyword="", full_text="not_requested"):
    if not item.get("id"):
        raise APIError("wb", "missing_note_id")
    identity = str(item["id"])
    user = item.get("user") or {}
    stamp, display, iso = date_fields(item.get("created_at"))
    payload = {"note_id": identity, "content": re.sub(r"<.*?>", "", item.get("text") or ""),
        "create_time": stamp, "create_date_time": display, "liked_count": str(item.get("attitudes_count", 0)),
        "comments_count": str(item.get("comments_count", 0)), "shared_count": str(item.get("reposts_count", 0)),
        "note_url": f"https://m.weibo.cn/detail/{identity}", "creator_hash": anonymize_user_id(user.get("id")),
        "nickname": mask_nickname(user.get("screen_name", "")), "source_keyword": keyword,
        "media": media_candidates(item)}
    return Record("wb", "note", identity, payload["note_url"], payload, source_date=iso,
        source_date_field="mblog.created_at", coverage={"author_masked": True,
        "full_text": full_text, "text_complete": not item.get("isLongText", False) or full_text in ("expanded", "detail_page"),
        "media_downloaded": False})


def normalize_comment(note, item, parent=""):
    if item.get("id") is None:
        raise APIError("wb", "missing_comment_id")
    stamp, display, iso = date_fields(item.get("created_at"))
    user, identity = item.get("user") or {}, str(item["id"])
    payload = {"comment_id": identity, "create_time": stamp, "create_date_time": display,
        "note_id": note, "content": re.sub(r"<.*?>", "", item.get("text") or ""),
        "sub_comment_count": str(item.get("total_number", 0)), "comment_like_count": str(item.get("like_count", 0)),
        "parent_comment_id": str(item.get("rootid") or parent),
        "creator_hash": anonymize_user_id(user.get("id")), "nickname": mask_nickname(user.get("screen_name", ""))}
    return Record("wb", "comment", identity, f"https://m.weibo.cn/detail/{note}#comment-{identity}", payload,
        source_date=iso, source_date_field="created_at", coverage={"author_masked": True, "embedded_reply": bool(parent)})


class WeiboClient(Client):
    async def get(self, path, params=None):
        body = self.json(await self.request("GET", "https://m.weibo.cn" + path, params=params))
        if body.get("ok") != 1:
            if body.get("errno") in (100001, "100001") or body.get("url", "").find("login") >= 0 or "登录" in str(body.get("msg", "")):
                raise NeedsLogin("wb", self.account)
            raise APIError("wb", "business_error")
        data = body.get("data")
        if not isinstance(data, dict):
            raise APIError("wb", "missing_data")
        return data

    async def detail(self, identity):
        response = await self.request("GET", f"https://m.weibo.cn/detail/{identity}")
        match = re.search(r"var \$render_data\s*=\s*(\[.*?\])\s*\[0\]", response.text, re.DOTALL)
        if not match:
            if "passport" in response.text or "登录" in response.text:
                raise NeedsLogin("wb", self.account)
            raise APIError("wb", "missing_render_data")
        data = json.loads(match[1])[0].get("status")
        if not isinstance(data, dict):
            raise APIError("wb", "missing_status")
        return data

    async def expand(self, item, keyword=""):
        state = "not_requested"
        if item.get("isLongText") and self.context.params.get("full_text", True):
            try:
                item, state = await self.detail(str(item["id"])), "expanded"
            except (APIError, NeedsLogin):
                state = "expansion_failed"
        return normalize_note(item, keyword=keyword, full_text=state)

    async def container(self, identity):
        response = await self.request("GET", f"https://m.weibo.cn/u/{identity}")
        value = response.cookies.get("M_WEIBOCN_PARAMS")
        if value:
            container = parse_qs(unquote(value)).get("lfid", [""])[0]
            if container:
                return container
        # The same official profile API exposes its post tab's actual container.
        data = await self.get("/api/container/getIndex", {"jumpfrom": "weibocom", "type": "uid", "value": identity, "containerid": f"100505{identity}"})
        for tab in (data.get("tabsInfo") or {}).get("tabs") or []:
            if tab.get("tab_type") == "weibo" and tab.get("containerid"):
                return str(tab["containerid"])
        raise APIError("wb", "missing_creator_container")


async def collect(context, *, transport=None, interval=1.0):
    from ...core.sessions import open_http
    params, coverage = context.params, []
    limits, mode = budgets(params), params.get("mode", "detail")
    http = await open_http(context, platform="wb", account=params.get("account"), transport=transport,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.weibo.cn/"})
    client = WeiboClient(http, context, "wb", interval=interval)
    if mode == "detail":
        for identity in map(note_id, selections(params, mode)):
            async def fetch(_, identity=identity):
                return [normalize_note(await client.detail(identity), full_text="detail_page")], None, True
            coverage.append(await walk(context, f"wb:detail:{identity}", fetch, **limits))
    elif mode == "search":
        search_types = {"default": "1", "real_time": "61", "popular": "60", "video": "64"}
        search_type = search_types.get(params.get("search_type", "default"), str(params.get("search_type")))
        if search_type not in search_types.values():
            raise ValueError("Invalid Weibo search_type")
        for keyword in keywords(params):
            async def fetch(page, keyword=keyword):
                data = await client.get("/api/container/getIndex", {"containerid": f"100103type={search_type}&q={keyword}", "page_type": "searchall", "page": page})
                if not isinstance(data.get("cards"), list):
                    raise APIError("wb", "missing_cards")
                items = cards(data["cards"])
                selected, truncated = bounded_items(fetch, items, "wb", "note", lambda item: item.get("id"))
                records = [await client.expand(item, keyword) for item in selected]
                return records, page + 1, not items, truncated
            coverage.append(await walk(context, f"wb:search:{keyword}:{search_type}", fetch, initial=int(params.get("start_page", 1)), **limits))
    elif mode == "creator":
        for identity in map(creator_id, selections(params, mode)):
            stream = f"wb:creator:{anonymize_user_id(identity)}"
            cp = await context.checkpoint(stream)
            container = None if cp and cp["exhausted"] else await client.container(identity)
            async def fetch(cursor, identity=identity, container=container):
                data = await client.get("/api/container/getIndex", {"jumpfrom": "weibocom", "type": "uid", "value": identity, "containerid": container, "since_id": cursor})
                if not isinstance(data.get("cards"), list):
                    raise APIError("wb", "missing_cards")
                items, info = cards(data["cards"]), data.get("cardlistInfo") or {}
                next_id = str(info.get("since_id", "0"))
                selected, truncated = bounded_items(fetch, items, "wb", "note", lambda item: item.get("id"))
                return [await client.expand(item) for item in selected], next_id, not items or next_id == "0", truncated
            coverage.append(await walk(context, stream, fetch, initial="", **limits))
    else:
        raise ValueError("Weibo modes: search, detail, creator")
    maximum, replies = int(params.get("max_comments", 0)), bool(params.get("include_replies", False))
    if maximum:
        for row in await observations(context, "wb", {"note"}):
            identity = row["native_id"]
            embedded_incomplete = False
            async def fetch(cursor, identity=identity):
                nonlocal embedded_incomplete
                query = {"id": identity, "mid": identity, "max_id_type": cursor[1]}
                if int(cursor[0]) > 0:
                    query["max_id"] = cursor[0]
                data = await client.get("/comments/hotflow", query)
                if "max_id" not in data or "max_id_type" not in data or not isinstance(data.get("data"), list):
                    raise APIError("wb", "invalid_comment_cursor")
                records = []
                for item in data["data"]:
                    records.append(normalize_comment(identity, item))
                    if replies:
                        children = item.get("comments") if isinstance(item.get("comments"), list) else []
                        children = children[:int(params.get("max_replies", maximum))]
                        records.extend(normalize_comment(identity, child, str(item["id"])) for child in children)
                        embedded_incomplete |= int(item.get("total_number") or 0) > len(children)
                return records, [data["max_id"], data["max_id_type"]], str(data["max_id"]) == "0"
            result = await walk(context, f"wb:comments:{identity}:{int(replies)}", fetch, initial=[-1, 0], maximum=maximum, page_limit=limits["page_limit"])
            if replies:
                # Source supports only embedded replies, never certify exhaustive retrieval.
                result.update(complete=False, reply_coverage="embedded_only", reason="embedded_replies_not_exhaustive")
            coverage.append(result)
    rows = await observations(context, "wb", {"note"})
    if any(not row["coverage"].get("text_complete", True) for row in rows):
        coverage.append({"complete": False, "reason": "truncated_long_text"})
    return summary("wb", mode, coverage, replies="embedded_only" if replies else "not_requested")
