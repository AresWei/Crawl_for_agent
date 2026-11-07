# Copyright (c) 2025 relakkes@gmail.com. NON-COMMERCIAL LEARNING LICENSE 1.1.
# Adapted from MediaCrawler 54a3d924 media_platform/kuaishou/{client,help}.py,
# store/kuaishou/__init__.py. Changes: shared browser/HTTP, atomic bounded resume.
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..core.jobs import NeedsLogin
from ..core.records import Record
from ..core.sessions import open_browser, open_http
from . import kuaishou_media as media
from .xiaohongshu_support import (Client, Page, PlatformError, UA, anonymous, keyword_batch, limits,
    masked, require_id, selected_values, stream, summary, timestamp)

CAPABILITIES = {"modes": ["search", "detail", "creator"], "comments": True, "replies": True,
    "pagination": True, "media": ["cover", "video"], "signing": "selected_browser_realm",
    "creator_profiles": False, "legacy_graphql": True, "live_validation": "pending_selected_account"}

# Source-preserved capture of the normal page's own signer; no challenge solver.
KS_SIGN_CAPTURE_SCRIPT = """
(() => {
  if (window.__ks_realm) return;
  let done = false;
  const setter = function (v) {
    if (!done && this && typeof this === "object" && this !== window &&
        typeof this.$encode === "function" && typeof this.$getCatVersion === "function") {
      done = true;
      window.__ks_realm = this;
      try { delete Object.prototype.caver; } catch (e) {}
    }
    Object.defineProperty(this, "caver", {value: v, writable: true, enumerable: true, configurable: true});
  };
  try { Object.defineProperty(Object.prototype, "caver", {set: setter, configurable: true}); } catch (e) {}
})();
"""


def parse_url(value, *, creator=False):
    value = str(value).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value): return value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"www.kuaishou.com", "kuaishou.com"}:
        raise ValueError("Expected an official Kuaishou URL or identifier")
    match = re.fullmatch(r"/(?:profile)/([A-Za-z0-9_-]+)/?" if creator else r"/short-video/([A-Za-z0-9_-]+)/?", parsed.path)
    if not match: raise ValueError("Invalid Kuaishou URL")
    return match[1]


def normalize_video(item, *, keyword=""):
    photo, author = item.get("photo") or {}, item.get("author") or {}
    identity = require_id(photo.get("id"), "ks")
    caption = photo.get("caption", "")
    url = f"https://www.kuaishou.com/short-video/{identity}"
    payload = {"video_id": identity, "video_type": str(item.get("type")), "title": caption[:500], "desc": caption[:500],
        "create_time": photo.get("timestamp"), "creator_hash": anonymous(author.get("id")), "nickname": masked(author.get("name")),
        "liked_count": str(photo.get("realLikeCount")), "viewd_count": str(photo.get("viewCount")),
        "last_modify_ts": int(time.time() * 1000), "video_url": url, "video_cover_url": media.extract_cover_url(item),
        "video_play_url": photo.get("photoUrl", ""), "source_keyword": keyword, "video_candidates": media.extract_video_urls(item)}
    payload["media"] = []
    if payload["video_cover_url"]: payload["media"].append({"url": payload["video_cover_url"], "kind": "image", "role": "cover"})
    if payload["video_candidates"]: payload["media"].append({"url": payload["video_candidates"][0], "kind": "video", "backup_urls": payload["video_candidates"][1:]})
    return Record("ks", "video", identity, url, payload, source_date=timestamp(photo.get("timestamp"), milliseconds=True),
        source_date_field="photo.timestamp", coverage={"author_masked": True, "description_limit": 500,
        "description_truncated": len(caption) > 500, "media_downloaded": False})


def normalize_comment(video_id, item, *, parent=""):
    identity = require_id(item.get("comment_id") or item.get("commentId"), "ks")
    payload = {"comment_id": identity, "create_time": item.get("timestamp"), "video_id": video_id,
        "content": item.get("content"), "creator_hash": anonymous(item.get("author_id") or item.get("authorId")),
        "nickname": masked(item.get("author_name") or item.get("authorName")),
        "sub_comment_count": str(item.get("commentCount") or item.get("subCommentCount", 0)),
        "last_modify_ts": int(time.time() * 1000), "parent_comment_id": str(item.get("reply_to_comment_id") or item.get("replyToCommentId") or parent),
        "root_comment_id": parent}
    if "likedCount" in item: payload["like_count"] = item["likedCount"]
    if "like_count" in item: payload["like_count"] = item["like_count"]
    return Record("ks", "comment", identity, f"https://www.kuaishou.com/short-video/{video_id}#comment-{identity}", payload,
        source_date=timestamp(item.get("timestamp"), milliseconds=True), source_date_field="timestamp", coverage={"author_masked": True})


def rest_page(data, items, cursor="pcursorV2"):
    if not isinstance(data.get(items), list) or cursor not in data:
        raise PlatformError("ks", "missing_pagination_fields")
    next_cursor = data[cursor]
    if not isinstance(next_cursor, str): raise PlatformError("ks", "invalid_cursor")
    return Page(data[items], next_cursor, next_cursor != "no_more")


class KuaishouClient(Client):
    host = "https://www.kuaishou.com"

    def __init__(self, http, context, *, interval=1, signer=None):
        super().__init__(http, context, "ks", interval=interval)
        self.signer, self.browser = signer, None

    async def sign(self, path, body):
        if self.signer: return await self.signer(path, {"caver": 2}, body)
        if self.browser is None:
            self.browser = await open_browser(self.context, platform="ks", account=self.context.params.get("account"),
                headless=True, init_scripts=[KS_SIGN_CAPTURE_SCRIPT])
            self.http.headers["User-Agent"] = await self.browser.page.evaluate("() => navigator.userAgent")
            await self.browser.page.goto(self.host, wait_until="domcontentloaded")
            try:
                await self.browser.page.wait_for_function("() => !!window.__ks_realm", timeout=15000)
            except Exception as error:
                # Do not confuse cancellation/budgets with an account challenge.
                if type(error).__name__ != "TimeoutError": raise
                raise NeedsLogin("ks", self.account, "Normal Kuaishou page signer unavailable; complete login/challenge") from None
            # Page-issued cookies remain scoped to this selected job/client.
            for cookie in await self.browser.context.cookies([self.host]):
                self.http.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))
        signature = await self.browser.page.evaluate("""([u,q,b]) => new Promise((resolve,reject) => {
            window.__ks_realm.call('$encode', [{url:u, query:q, form:{}, requestBody:b},
                {suc:s => resolve(s), err:e => reject(new Error('sign_failed'))}]);
        })""", [path, {"caver": 2}, body])
        if not isinstance(signature, str) or not signature: raise PlatformError("ks", "signature_missing")
        return signature

    async def rest(self, path, body, *, signed=False):
        params = {"__NS_hxfalcon": await self.sign(path, body), "caver": 2} if signed else None
        data = await self.json("POST", self.host + path, params=params,
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        if data.get("result") in {50, 1001, 1002}:
            raise NeedsLogin("ks", self.account)
        if data.get("result") != 1:
            raise PlatformError("ks", data.get("result", "missing_result"))
        return data

    async def graphql(self, query, operation, variables):
        source = (Path(__file__).with_name("kuaishou_assets") / (query + ".graphql")).read_text(encoding="utf-8")
        data = await self.json("POST", self.host + "/graphql", json={"operationName": operation, "variables": variables, "query": source})
        if data.get("errors") or not isinstance(data.get("data"), dict):
            raise PlatformError("ks", "graphql_error")
        result = data["data"].get(operation)
        if not isinstance(result, dict): raise PlatformError("ks", "graphql_result_missing")
        if "result" in result and result["result"] != 1:
            if result["result"] == 50: raise NeedsLogin("ks", self.account)
            raise PlatformError("ks", result["result"])
        return result

    async def detail(self, identity):
        result = await self.graphql("video_detail", "visionVideoDetail", {"photoId": identity, "page": "search"})
        if not (result.get("photo") or {}).get("id"): raise PlatformError("ks", "video_unavailable")
        if str(result["photo"]["id"]) != identity: raise PlatformError("ks", "detail_identity_mismatch")
        return result


async def comments(context, client, identity, budget):
    if budget["max_comments"] == 0: return {"complete": True, "reason": "not_requested"}
    async def fetch(cursor):
        return rest_page(await client.rest("/rest/v/photo/comment/list", {"photoId": identity, "pcursor": cursor}), "rootCommentsV2")
    async def child(item, record):
        if not context.params.get("replies", context.params.get("include_replies", False)) or not item.get("hasSubComments", False):
            return {"complete": True}
        async def fetch_reply(cursor):
            if not record.native_id.isdigit(): raise PlatformError("ks", "invalid_root_comment_id")
            return rest_page(await client.rest("/rest/v/photo/comment/sublist", {"photoId": identity,
                "pcursor": cursor, "rootCommentId": int(record.native_id)}), "subCommentsV2")
        return await stream(context, f"ks:replies:{identity}:{record.native_id}", fetch_reply,
            lambda value: normalize_comment(identity, value, parent=record.native_id), maximum=budget["max_replies"], page_limit=budget["max_pages"])
    return await stream(context, f"ks:comments:{identity}", fetch, lambda value: normalize_comment(identity, value),
        maximum=budget["max_comments"], page_limit=budget["max_pages"], child=child)


async def collect(context, *, transport=None, interval=1.0):
    expanded = await keyword_batch(context, collect, transport=transport, interval=interval)
    if expanded is not None: return expanded
    params, budget = context.params, limits(context.params)
    mode = params.get("mode", "detail")
    if mode not in CAPABILITIES["modes"]: raise ValueError("Kuaishou supports search, detail, creator")
    http = await open_http(context, platform="ks", account=params.get("account"), transport=transport,
        headers={"User-Agent": UA, "Referer": "https://www.kuaishou.com/", "Origin": "https://www.kuaishou.com", "Content-Type": "application/json"})
    client = KuaishouClient(http, context, interval=interval)
    results = []
    async def normalize(item):
        identity = require_id((item.get("photo") or {}).get("id") or item.get("id"), "ks")
        return normalize_video(await client.detail(identity), keyword=params.get("keyword", ""))
    async def child(item, record): return await comments(context, client, record.native_id, budget)
    if mode == "detail":
        values = []
        for value in selected_values(params, mode):
            parsed = urlsplit(str(value))
            if parsed.hostname in {"www.kuaishou.com", "kuaishou.com"} and parsed.path.startswith("/f/"):
                value = await client.resolve(value, {"www.kuaishou.com", "kuaishou.com"})
            values.append({"id": parse_url(value)})
        async def fetch(_): return Page(values, None, False)
        results.append(await stream(context, "ks:detail", fetch, normalize, maximum=budget["max_posts"], page_limit=1, child=child))
    elif mode == "search":
        keyword = str(params.get("keyword", "")).strip()
        if not keyword: raise ValueError("keyword is required")
        async def fetch(cursor):
            if params.get("api_variant") == "graphql":
                data = await client.graphql("search_query", "visionSearchPhoto", {"keyword": keyword,
                    "pcursor": cursor["page"], "page": "search", "searchSessionId": cursor["session"]})
            else:
                data = await client.rest("/rest/v/search/feed", {"keyword": keyword, "pcursor": cursor["page"],
                    "page": "search", "searchSessionId": cursor["session"]}, signed=True)
            result = rest_page(data, "feeds", "pcursor")
            result.cursor = {"page": result.cursor, "session": data.get("searchSessionId", cursor["session"])}
            return result
        results.append(await stream(context, "ks:search:" + anonymous(keyword), fetch, normalize,
            initial={"page": str(max(1, int(params.get("start_page", 1)))), "session": ""},
            maximum=budget["max_posts"], page_limit=budget["max_pages"], child=child))
    else:
        remaining = budget["max_posts"]
        for value in selected_values(params, mode):
            identity = parse_url(value, creator=True)
            if remaining <= 0:
                results.append({"stream": "ks:creator:" + anonymous(identity), "count": 0, "complete": False, "reason": "task_post_limit"})
                continue
            async def fetch(cursor, identity=identity):
                if params.get("api_variant") == "graphql":
                    data = await client.graphql("vision_profile_photo_list", "visionProfilePhotoList", {"page": "profile", "pcursor": cursor, "userId": identity})
                else:
                    data = await client.rest("/rest/v/profile/feed", {"user_id": identity, "pcursor": cursor, "page": "profile"}, signed=True)
                return rest_page(data, "feeds", "pcursor")
            results.append(await stream(context, "ks:creator:" + anonymous(identity), fetch, normalize,
                maximum=remaining, page_limit=budget["max_pages"], child=child))
            remaining -= results[-1]["count"]
    result = summary("ks", results)
    if transport is not None: result["evidence"] = "injected_transport"
    return result
