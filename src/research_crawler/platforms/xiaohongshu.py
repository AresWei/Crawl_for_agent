# Copyright (c) 2025 relakkes@gmail.com. NON-COMMERCIAL LEARNING LICENSE 1.1.
# Source: MediaCrawler 54a3d924, media_platform/xhs/{client,extractor,help}.py,
# store/xhs/__init__.py. Changes: injected sessions, pure records, bounded resume.
from __future__ import annotations

import json
import re
import secrets
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ..core.jobs import NeedsLogin
from ..core.records import Record
from ..core.sessions import open_http
from . import xiaohongshu_media as media
from .xiaohongshu_support import (Client, Page, PlatformError, UA, anonymous, keyword_batch, limits,
    masked, page, require_id, selected_values, stream, summary, timestamp)

CAPABILITIES = {"modes": ["search", "detail", "creator"], "comments": True, "replies": True,
    "pagination": True, "media": ["image", "cover", "video", "live_photo"],
    "signing": "local_xhshow", "creator_profiles": False, "international": True,
    "live_validation": "pending_selected_account"}


def parse_url(value, *, creator=False):
    value = str(value).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return {"id": value, "token": "", "source": "pc_feed" if creator else "pc_search"}
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com", "www.rednote.com", "rednote.com"}:
        raise ValueError("Expected an official Xiaohongshu/Rednote URL or identifier")
    pattern = r"/user/profile/([A-Za-z0-9_-]+)" if creator else r"/(?:explore|discovery/item)/([A-Za-z0-9_-]+)"
    match = re.fullmatch(pattern + r"/?", parsed.path)
    if not match:
        raise ValueError("Invalid Xiaohongshu URL")
    query = parse_qs(parsed.query)
    return {"id": match[1], "token": query.get("xsec_token", [""])[0],
            "source": query.get("xsec_source", ["pc_feed" if creator else "pc_search"])[0]}


def extract_html(note_id, html):
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>", html, re.S)
    if not match:
        raise PlatformError("xhs", "html_note_state_missing")
    # Upstream's JSON state contains JavaScript undefined values. Only replace
    # value tokens outside strings, preserving literal text containing undefined.
    raw = re.sub(r'("(?:\\.|[^"\\])*"|\bundefined\b)', lambda m: 'null' if m[0] == 'undefined' else m[0], match[1])
    state = json.loads(raw)
    def snake(value):
        if isinstance(value, dict):
            return {re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): snake(child) for key, child in value.items()}
        return [snake(child) for child in value] if isinstance(value, list) else value
    state = snake(state)
    note = ((state.get("note") or {}).get("note_detail_map") or {}).get(note_id, {}).get("note")
    if not isinstance(note, dict):
        raise PlatformError("xhs", "html_note_missing")
    return note


def search_id():
    # Source get_search_id: millisecond timestamp shifted 64 bits plus random31,
    # then base36. Keep the source shape instead of inventing a UUID token.
    value = (int(time.time() * 1000) << 64) + secrets.randbelow(2147483647)
    chars, result = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", ""
    while value:
        value, remainder = divmod(value, 36)
        result = chars[remainder] + result
    return result


def normalize_note(item, *, keyword="", international=False):
    identity = require_id(item.get("note_id") or item.get("id"), "xhs")
    user, interaction = item.get("user") or {}, item.get("interact_info") or {}
    host = "www.rednote.com" if international else "www.xiaohongshu.com"
    token, source = item.get("xsec_token", ""), item.get("xsec_source") or "pc_search"
    url = f"https://{host}/explore/{identity}?" + urlencode({"xsec_token": token, "xsec_source": source})
    videos, images = media.extract_video_urls(item, international), media.extract_image_urls(item)
    payload = {"note_id": identity, "type": item.get("type"), "title": item.get("title") or item.get("desc", "")[:255],
        "desc": item.get("desc", ""), "video_url": ",".join(videos), "time": item.get("time"),
        "last_update_time": item.get("last_update_time", 0), "creator_hash": anonymous(user.get("user_id")),
        "nickname": masked(user.get("nickname")), "image_list": ",".join(images),
        "tag_list": ",".join(tag.get("name", "") for tag in item.get("tag_list", []) if tag.get("type") == "topic"),
        "last_modify_ts": int(time.time() * 1000), "note_url": url, "source_keyword": keyword, "xsec_token": token,
        "xsec_source": source, "cover_url": media.extract_cover_url(item), "video_candidates": videos,
        "live_photos": [{"index": index, "image_url": media._image_url_of(image),
                          "video_candidates": media.extract_live_photo_video_urls(image)}
                        for index, image in enumerate(item.get("image_list") or [], 1) if image.get("live_photo")]}
    payload.update({key: interaction.get(key) for key in ("liked_count", "collected_count", "comment_count", "share_count")})
    payload["media"] = []
    if item.get("type") == "video":
        if payload["cover_url"]: payload["media"].append({"url": payload["cover_url"], "kind": "image", "role": "cover"})
        if videos: payload["media"].append({"url": videos[0], "kind": "video", "backup_urls": videos[1:]})
    else:
        payload["media"].extend({"url": media._image_url_of(image), "kind": "image", "index": index}
            for index, image in enumerate(item.get("image_list") or [], 1) if media._image_url_of(image))
        payload["media"].extend({"url": live["video_candidates"][0], "kind": "live_photo", "index": live["index"],
            "image_url": live["image_url"], "backup_urls": live["video_candidates"][1:]} for live in payload["live_photos"] if live["video_candidates"])
    return Record("xhs", "note", identity, url, payload, source_date=timestamp(item.get("time"), milliseconds=True),
        source_date_field="time", coverage={"author_masked": True, "media_downloaded": False, "detail_source": item.get("_detail_source", "feed")})


def normalize_comment(note_id, item, *, parent=""):
    identity = require_id(item.get("id"), "xhs")
    user = item.get("user_info") or {}
    payload = {"comment_id": identity, "create_time": item.get("create_time"), "note_id": note_id,
        "content": item.get("content"), "creator_hash": anonymous(user.get("user_id")), "nickname": masked(user.get("nickname")),
        "sub_comment_count": item.get("sub_comment_count", 0), "pictures": ",".join(image.get("url_default", "") for image in item.get("pictures") or []),
        "parent_comment_id": (item.get("target_comment") or {}).get("id") or parent, "root_comment_id": parent,
        "last_modify_ts": int(time.time() * 1000), "like_count": item.get("like_count", 0)}
    return Record("xhs", "comment", identity, f"https://www.xiaohongshu.com/explore/{note_id}#comment-{identity}", payload,
        source_date=timestamp(item.get("create_time"), milliseconds=True), source_date_field="create_time", coverage={"author_masked": True})


class XiaohongshuClient(Client):
    def __init__(self, http, context, *, interval=1, signer=None):
        super().__init__(http, context, "xhs", interval=interval)
        self.international = bool(context.params.get("international", False))
        self.host = "https://webapi.rednote.com" if self.international else "https://edith.xiaohongshu.com"
        self.domain = "https://www.rednote.com" if self.international else "https://www.xiaohongshu.com"
        self.signer = signer

    async def api(self, path, values, *, method="GET"):
        cookies = self.cookies(self.host + path)
        if not cookies:
            raise NeedsLogin("xhs", self.account)
        from ..signing.client import backend, service_sign
        signing_backend = backend(self.context)
        if signing_backend == 'service':
            headers = await service_sign(self.context, 'xhs', 'headers',
                {'path': path, 'method': method, 'cookies': cookies, 'values': values})
        elif self.signer is None:
            try:
                from xhshow import Xhshow
            except ImportError:
                raise PlatformError("xhs", "dependency_xhshow_missing") from None
            self.signer = Xhshow()
        if signing_backend == 'local':
            sign = self.signer.sign_headers_post if method == "POST" else self.signer.sign_headers_get
            headers = sign(uri=path, cookies=cookies, **({"payload": values} if method == "POST" else {"params": values}))
        if not headers.get("x-s") or not headers.get("x-t"):
            raise PlatformError("xhs", "signature_missing")
        if method == "GET":
            query = "&".join(f"{key}={quote(str(value) if value is not None else '', safe=',')}" for key, value in values.items())
            data = await self.json("GET", self.host + path + ("?" + query if query else ""), headers=headers)
        else:
            data = await self.json("POST", self.host + path, headers=headers,
                content=json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode())
        if data.get("code") in {-100, -101, 300011}:
            raise NeedsLogin("xhs", self.account)
        if data.get("success") is not True or not isinstance(data.get("data"), dict):
            raise PlatformError("xhs", data.get("code", "invalid_response"))
        return data["data"]

    async def detail(self, info):
        async def html_detail():
            response = await self.request("GET", self.domain + "/explore/" + info["id"], params={"xsec_token": info["token"], "xsec_source": info["source"]})
            note = extract_html(info["id"], response.text)
            note["_detail_source"] = "html"
            return note
        if self.context.params.get("detail_source") == "html":
            note = await html_detail()
        else:
            data = await self.api("/api/sns/web/v1/feed", {"source_note_id": info["id"], "image_formats": ["jpg", "webp", "avif"],
                "extra": {"need_body_topic": 1}, "xsec_source": info["source"], "xsec_token": info["token"]}, method="POST")
            items = data.get("items") or []
            if not items or not isinstance(items[0].get("note_card"), dict):
                # Frozen core.py falls back to normal HTML when feed is empty;
                # explicit deleted/security response codes are never bypassed.
                note = await html_detail()
            else:
                note = dict(items[0]["note_card"])
        if note.get("note_id") and str(note["note_id"]) != info["id"]:
            raise PlatformError("xhs", "detail_identity_mismatch")
        note.update(note_id=info["id"], xsec_token=info["token"], xsec_source=info["source"])
        return note


async def comments(context, client, note_id, token, budget):
    if budget["max_comments"] == 0:
        return {"complete": True, "reason": "not_requested"}
    async def roots(cursor):
        data = await client.api("/api/sns/web/v2/comment/page", {"note_id": note_id, "cursor": cursor, "top_comment_id": "", "image_formats": "jpg,webp,avif", "xsec_token": token})
        return page(data, "comments")
    async def children(item, record):
        if not context.params.get("replies", context.params.get("include_replies", False)):
            return {"complete": True}
        embedded, more = item.get("sub_comments") or [], bool(item.get("sub_comment_has_more"))
        if not embedded and not more:
            return {"complete": True}
        async def fetch(cursor):
            if cursor == {"embedded": True}:
                return Page(embedded, item.get("sub_comment_cursor", ""), more)
            data = await client.api("/api/sns/web/v2/comment/sub/page", {"note_id": note_id, "root_comment_id": record.native_id,
                "num": "10", "cursor": cursor, "image_formats": "jpg,webp,avif", "top_comment_id": "", "xsec_token": token})
            return page(data, "comments")
        return await stream(context, f"xhs:replies:{note_id}:{record.native_id}", fetch,
            lambda value: normalize_comment(note_id, value, parent=record.native_id), initial={"embedded": True},
            maximum=budget["max_replies"], page_limit=budget["max_pages"])
    return await stream(context, f"xhs:comments:{note_id}", roots, lambda value: normalize_comment(note_id, value),
        maximum=budget["max_comments"], page_limit=budget["max_pages"], child=children)


async def collect(context, *, transport=None, interval=1.0):
    expanded = await keyword_batch(context, collect, transport=transport, interval=interval)
    if expanded is not None: return expanded
    params, budget = context.params, limits(context.params)
    mode = params.get("mode", "detail")
    if mode not in CAPABILITIES["modes"]:
        raise ValueError("Xiaohongshu supports search, detail, creator")
    international = bool(params.get("international", False))
    domain = "https://www.rednote.com" if international else "https://www.xiaohongshu.com"
    http = await open_http(context, platform="xhs", account=params.get("account"), transport=transport,
        headers={"User-Agent": UA, "Origin": domain, "Referer": domain + "/", "Content-Type": "application/json;charset=UTF-8"})
    client = XiaohongshuClient(http, context, interval=interval)
    results = []
    async def normalize(item):
        info = {"id": require_id(item.get("note_id") or item.get("id"), "xhs"), "token": item.get("xsec_token", ""), "source": item.get("xsec_source") or "pc_search"}
        return normalize_note(await client.detail(info), keyword=params.get("keyword", ""), international=international)
    async def child(item, record):
        return await comments(context, client, record.native_id, record.payload["xsec_token"], budget)
    if mode == "detail":
        values = []
        for value in selected_values(params, mode):
            if urlsplit(str(value)).hostname in {"xhslink.com", "www.xhslink.com"}:
                value = await client.resolve(value, {"xhslink.com", "www.xhslink.com", "www.xiaohongshu.com", "xiaohongshu.com"})
            info = parse_url(value)
            values.append({"id": info["id"], "xsec_token": info["token"], "xsec_source": info["source"]})
        async def fetch(_): return Page(values, None, False)
        results.append(await stream(context, "xhs:detail", fetch, normalize, maximum=budget["max_posts"], page_limit=1, child=child))
    elif mode == "search":
        keyword = str(params.get("keyword", "")).strip()
        if not keyword: raise ValueError("keyword is required")
        sort = params.get("sort", "general")
        if sort not in {"general", "popularity_descending", "time_descending"}: raise ValueError("Invalid XHS sort")
        async def fetch(cursor):
            data = await client.api("/api/sns/web/v1/search/notes", {"keyword": keyword, "page": cursor["page"], "page_size": 20,
                "search_id": cursor["search_id"], "sort": sort, "note_type": int(params.get("note_type", 0))}, method="POST")
            raw = page(data, "items", cursor={"page": cursor["page"] + 1, "search_id": cursor["search_id"]})
            raw.items = [item for item in raw.items if item.get("model_type") not in {"hot_query", "query"} and (item.get("id") or item.get("note_id"))]
            return raw
        results.append(await stream(context, "xhs:search:" + anonymous(keyword), fetch, normalize,
            initial={"page": max(1, int(params.get("start_page", 1))), "search_id": search_id()}, maximum=budget["max_posts"], page_limit=budget["max_pages"], child=child))
    else:
        remaining = budget["max_posts"]
        for value in selected_values(params, mode):
            info = parse_url(value, creator=True)
            if remaining <= 0:
                results.append({"stream": "xhs:creator:" + anonymous(info["id"]), "count": 0, "complete": False, "reason": "task_post_limit"})
                continue
            async def fetch(cursor, info=info):
                data = await client.api("/api/sns/web/v1/user_posted", {"num": 30, "cursor": cursor, "user_id": info["id"],
                    "image_formats": "jpg,webp,avif", "xsec_token": info["token"], "xsec_source": info["source"]})
                return page(data, "notes")
            results.append(await stream(context, "xhs:creator:" + anonymous(info["id"]), fetch, normalize,
                maximum=remaining, page_limit=budget["max_pages"], child=child))
            remaining -= results[-1]["count"]
    result = summary("xhs", results)
    if transport is not None: result["evidence"] = "injected_transport"
    return result
