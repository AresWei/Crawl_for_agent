# Copyright (c) 2025 relakkes@gmail.com. NON-COMMERCIAL LEARNING LICENSE 1.1.
# Adapted from MediaCrawler 54a3d924 media_platform/douyin/{client,help}.py,
# store/douyin/__init__.py. Local JS retains ShilongLee's original attribution.
# Changes: one scoped computation worker, shared HTTP, atomic bounded pagination.
from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ..core.jobs import NeedsLogin
from ..core.records import Record
from ..core.sessions import open_http
from . import douyin_media as media
from .xiaohongshu_support import (Client, Page, PlatformError, UA, anonymous, keyword_batch, limits,
    masked, page, require_id, selected_values, stream, summary, timestamp)

CAPABILITIES = {"modes": ["search", "detail", "creator"], "comments": True, "replies": True,
    "pagination": True, "media": ["image", "cover", "video", "live_photo"],
    "signing": "local_node_a_bogus_GET", "creator_profiles": False,
    "live_validation": "pending_selected_account"}


def parse_url(value, *, creator=False):
    value = str(value).strip()
    if (creator and re.fullmatch(r"[A-Za-z0-9_-]+", value)) or (not creator and value.isdigit()):
        return value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"douyin.com", "www.douyin.com", "www.iesdouyin.com", "iesdouyin.com"}:
        raise ValueError("Expected an official Douyin URL or identifier")
    if not creator:
        modal = parse_qs(parsed.query).get("modal_id", [""])[0]
        if modal.isdigit(): return modal
    match = re.search(r"/user/([A-Za-z0-9_-]+)" if creator else r"/(?:video|note)/(\d+)", parsed.path)
    if not match: raise ValueError("Invalid Douyin URL")
    return match[1]


class DouyinSigner:
    def __init__(self, context):
        self.context, self.process, self.lock = context, None, asyncio.Lock()

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()

    async def sign(self, path, query, ua):
        from ..signing.client import backend, service_sign
        if backend(self.context) == 'service':
            return (await service_sign(self.context, 'dy', 'a_bogus',
                    {'path': path, 'query': query, 'ua': ua}))['a_bogus']
        async with self.lock:
            if self.process is None:
                node = shutil.which("node")
                if not node: raise PlatformError("dy", "dependency_node_missing")
                self.process = await asyncio.create_subprocess_exec(node, str(Path(__file__).with_name("douyin_sign_runner.js")),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env=self.context.paths.child_environment(),
                    **({"creationflags": 0x08000000} if __import__("os").name == "nt" else {}))
                self.context.resources.push_async_callback(self.close)
            value = {"fn": "sign_reply" if "/reply" in path else "sign_datail", "query": query, "ua": ua}
            self.process.stdin.write((json.dumps(value) + "\n").encode())
            await self.process.stdin.drain()
            try:
                result = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 10))
            except (ValueError, asyncio.TimeoutError):
                raise PlatformError("dy", "local_signature_failed") from None
            if not isinstance(result.get("signature"), str) or not result["signature"]:
                raise PlatformError("dy", "local_signature_missing")
            return result["signature"]


def normalize_video(item, *, keyword=""):
    identity = require_id(item.get("aweme_id"), "dy")
    user, interaction = item.get("author") or {}, item.get("statistics") or {}
    videos, images = media.extract_video_urls(item), media.extract_image_urls(item)
    url = f"https://www.douyin.com/video/{identity}"
    payload = {"aweme_id": identity, "aweme_type": str(item.get("aweme_type")), "title": item.get("desc", ""),
        "desc": item.get("desc", ""), "create_time": item.get("create_time"), "creator_hash": anonymous(user.get("uid")),
        "nickname": masked(user.get("nickname")), "last_modify_ts": int(time.time() * 1000), "aweme_url": url,
        "cover_url": media.extract_cover_url(item), "video_download_url": (videos or [""])[0],
        "music_download_url": ((item.get("music") or {}).get("play_url") or {}).get("uri", ""),
        "note_download_url": ",".join(images), "source_keyword": keyword, "video_candidates": videos,
        "live_photos": [{"index": index, "image_url": (media._url_list_of(image) or [""])[-1],
                          "video_candidates": media.extract_live_photo_video_urls(image)}
                         for index, image in enumerate(item.get("images") or [], 1) if isinstance(image.get("video"), dict)]}
    for out, source in (("liked_count", "digg_count"), ("collected_count", "collect_count"), ("comment_count", "comment_count"), ("share_count", "share_count")):
        payload[out] = str(interaction.get(source))
    payload["media"] = [{"url": media._url_list_of(image)[-1], "kind": "image", "index": index}
        for index, image in enumerate(item.get("images") or [], 1) if media._url_list_of(image)]
    if not images:
        if payload["cover_url"]: payload["media"].append({"url": payload["cover_url"], "kind": "image", "role": "cover"})
        if videos: payload["media"].append({"url": videos[0], "kind": "video", "backup_urls": videos[1:]})
    payload["media"].extend({"url": live["video_candidates"][0], "kind": "live_photo", "index": live["index"],
        "image_url": live["image_url"], "backup_urls": live["video_candidates"][1:]} for live in payload["live_photos"] if live["video_candidates"])
    return Record("dy", "video", identity, url, payload, source_date=timestamp(item.get("create_time")),
        source_date_field="create_time", coverage={"author_masked": True, "media_downloaded": False})


def normalize_comment(video_id, item, *, parent=""):
    identity = require_id(item.get("cid"), "dy")
    if item.get("aweme_id") is not None and str(item["aweme_id"]) != video_id:
        raise PlatformError("dy", "comment_content_mismatch")
    user, images = item.get("user") or {}, []
    for image in item.get("image_list") or []:
        candidates = media._url_list_of(image.get("origin_url"))
        if candidates: images.append(candidates[1] if len(candidates) > 1 else candidates[0])
    payload = {"comment_id": identity, "create_time": item.get("create_time"), "aweme_id": video_id,
        "content": item.get("text"), "creator_hash": anonymous(user.get("uid")), "nickname": masked(user.get("nickname")),
        "sub_comment_count": str(item.get("reply_comment_total", 0)), "like_count": item.get("digg_count") or 0,
        "last_modify_ts": int(time.time() * 1000), "parent_comment_id": item.get("reply_id") or parent or "0",
        "root_comment_id": parent, "pictures": ",".join(images)}
    return Record("dy", "comment", identity, f"https://www.douyin.com/video/{video_id}#comment-{identity}", payload,
        source_date=timestamp(item.get("create_time")), source_date_field="create_time", coverage={"author_masked": True})


class DouyinClient(Client):
    host = "https://www.douyin.com"

    def __init__(self, http, context, *, interval=1, signer=None):
        super().__init__(http, context, "dy", interval=interval)
        self.signer = signer or DouyinSigner(context)
        cookies = SimpleCookie()
        cookies.load(self.cookies(self.host))
        self.cookie_values = {key: value.value for key, value in cookies.items()}
        self.webid = str(secrets.randbelow(10**19)).zfill(19)
        self.ms_token = self.cookie_values.get("msToken", "")
        from ..core.accounts import AccountStore
        from ..core.account_pool import pinned_identity
        identity = pinned_identity(context, 'dy', context.params.get('account'))
        state = identity['account_state'] if identity else AccountStore(context.paths).state("dy", context.params.get("account"))
        for origin in state.get("origins", []):
            if origin.get("origin") == self.host:
                self.ms_token = next((item["value"] for item in origin.get("localStorage", []) if item.get("name") == "xmst"), self.ms_token)

    async def api(self, path, params, *, headers=None):
        values = dict(params)
        values.update({"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web", "version_code": "190600",
            "version_name": "19.6.0", "update_version_code": "170400", "pc_client_type": "1", "cookie_enabled": "true",
            "browser_language": "zh-CN", "browser_platform": "Win32", "browser_name": "Chrome", "browser_version": "125.0.0.0",
            "browser_online": "true", "engine_name": "Blink", "os_name": "Windows", "os_version": "10", "cpu_core_num": "8",
            "device_memory": "8", "engine_version": "125.0", "platform": "PC", "screen_width": "1536", "screen_height": "864",
            "effective_type": "4g", "round_trip_time": "50", "webid": self.webid, "msToken": self.ms_token})
        if "/v1/web/general/search" not in path:
            values["a_bogus"] = await self.signer.sign(path, urlencode(values), self.http.headers.get("User-Agent", UA))
        data = await self.json("GET", self.host + path, params=values, headers=headers)
        code = data.get("status_code")
        if code in {8, 1008, 1024, 2190008}:
            raise NeedsLogin("dy", self.account)
        if code != 0:
            raise PlatformError("dy", code if code is not None else "missing_status_code")
        return data

    async def detail(self, identity):
        fp = self.cookie_values.get("s_v_web_id", "")
        data = await self.api("/aweme/v1/web/aweme/detail/", {"aweme_id": identity,
            "uifid": self.cookie_values.get("UIFID") or self.cookie_values.get("UIFID_TEMP", ""), "verifyFp": fp, "fp": fp})
        item = data.get("aweme_detail")
        if not isinstance(item, dict) or not item.get("aweme_id"):
            raise PlatformError("dy", "video_not_found")
        if str(item["aweme_id"]) != identity: raise PlatformError("dy", "detail_identity_mismatch")
        return item


async def comments(context, client, identity, budget):
    if budget["max_comments"] == 0: return {"complete": True, "reason": "not_requested"}
    async def fetch(cursor):
        return page(await client.api("/aweme/v1/web/comment/list/", {"aweme_id": identity, "cursor": cursor, "count": 20, "item_type": 0}), "comments")
    async def child(item, record):
        if not context.params.get("replies", context.params.get("include_replies", False)) or int(item.get("reply_comment_total") or 0) <= 0:
            return {"complete": True}
        async def fetch_reply(cursor):
            return page(await client.api("/aweme/v1/web/comment/list/reply/", {"comment_id": record.native_id,
                "cursor": cursor, "count": 20, "item_type": 0, "item_id": identity}), "comments")
        return await stream(context, f"dy:replies:{identity}:{record.native_id}", fetch_reply,
            lambda value: normalize_comment(identity, value, parent=record.native_id), initial=0,
            maximum=budget["max_replies"], page_limit=budget["max_pages"])
    return await stream(context, f"dy:comments:{identity}", fetch, lambda value: normalize_comment(identity, value),
        initial=0, maximum=budget["max_comments"], page_limit=budget["max_pages"], child=child)


async def collect(context, *, transport=None, interval=1.0):
    expanded = await keyword_batch(context, collect, transport=transport, interval=interval)
    if expanded is not None: return expanded
    params, budget = context.params, limits(context.params)
    mode = params.get("mode", "detail")
    if mode not in CAPABILITIES["modes"]: raise ValueError("Douyin supports search, detail, creator")
    http = await open_http(context, platform="dy", account=params.get("account"), transport=transport,
        headers={"User-Agent": UA, "Referer": "https://www.douyin.com/", "x-tt-argus": "1"})
    client = DouyinClient(http, context, interval=interval)
    uifid = client.cookie_values.get("UIFID") or client.cookie_values.get("UIFID_TEMP")
    if uifid: http.headers["uifid"] = uifid
    # Explicit optional browser environment, sharing selected account and lifecycle.
    if params.get("browser_signing_state"):
        from ..core.sessions import open_browser
        session = await open_browser(context, platform="dy", account=params.get("account"), headless=True)
        await session.page.goto(client.host, wait_until="domcontentloaded")
        client.ms_token = await session.page.evaluate("() => localStorage.getItem('xmst') || ''")
        for cookie in await session.context.cookies([client.host]):
            http.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))
            client.cookie_values[cookie["name"]] = cookie["value"]
        uifid = client.cookie_values.get("UIFID") or client.cookie_values.get("UIFID_TEMP")
        if uifid: http.headers["uifid"] = uifid
    results = []
    async def normalize(item):
        return normalize_video(await client.detail(require_id(item.get("aweme_id"), "dy")), keyword=params.get("keyword", ""))
    async def child(item, record): return await comments(context, client, record.native_id, budget)
    if mode == "detail":
        values = []
        for value in selected_values(params, mode):
            if urlsplit(str(value)).hostname == "v.douyin.com":
                value = await client.resolve(value, {"v.douyin.com", "www.douyin.com", "douyin.com", "www.iesdouyin.com", "iesdouyin.com"})
            values.append({"aweme_id": parse_url(value)})
        async def fetch(_): return Page(values, None, False)
        results.append(await stream(context, "dy:detail", fetch, normalize, maximum=budget["max_posts"], page_limit=1, child=child))
    elif mode == "search":
        keyword = str(params.get("keyword", "")).strip()
        if not keyword: raise ValueError("keyword is required")
        async def fetch(cursor):
            values = {"search_channel": params.get("search_channel", "aweme_general"), "enable_history": "1", "keyword": keyword,
                "search_source": "tab_search", "query_correct_type": "1", "is_filter_search": "0", "from_group_id": "7378810571505847586",
                "offset": cursor["offset"], "count": "15", "need_filter_settings": "1", "list_type": "multi", "search_id": cursor["search_id"]}
            if params.get("sort", 0) or params.get("publish_time", 0):
                values.update(is_filter_search=1, filter_selected=json.dumps({"sort_type": str(params.get("sort", 0)), "publish_time": str(params.get("publish_time", 0))}))
            data = await client.api("/aweme/v1/web/general/search/single/", values,
                headers={"Referer": "https://www.douyin.com/search/" + quote(keyword)})
            if "cursor" not in data: raise PlatformError("dy", "missing_search_cursor")
            result = page(data, "data", cursor={"offset": data["cursor"], "search_id": (data.get("extra") or {}).get("logid") or (data.get("log_pb") or {}).get("impr_id") or data.get("search_id") or cursor["search_id"]})
            items = []
            for item in result.items:
                if isinstance(item.get("aweme_info"), dict): items.append(item["aweme_info"])
                elif isinstance(item.get("aweme_mix_info"), dict): items.extend(item["aweme_mix_info"].get("mix_items") or [])
            result.items = items
            return result
        results.append(await stream(context, "dy:search:" + anonymous(keyword), fetch, normalize,
            initial={"offset": max(0, int(params.get("start_page", 1)) - 1) * 15, "search_id": ""},
            maximum=budget["max_posts"], page_limit=budget["max_pages"], child=child))
    else:
        remaining = budget["max_posts"]
        for value in selected_values(params, mode):
            identity = parse_url(value, creator=True)
            if remaining <= 0:
                results.append({"stream": "dy:creator:" + anonymous(identity), "count": 0, "complete": False, "reason": "task_post_limit"})
                continue
            async def fetch(cursor, identity=identity):
                data = await client.api("/aweme/v1/web/aweme/post/", {"sec_user_id": identity, "count": 18,
                    "max_cursor": cursor, "locate_query": "false", "publish_video_strategy_type": 2})
                return page(data, "aweme_list", cursor_key="max_cursor")
            results.append(await stream(context, "dy:creator:" + anonymous(identity), fetch, normalize,
                maximum=remaining, page_limit=budget["max_pages"], child=child))
            remaining -= results[-1]["count"]
    result = summary("dy", results)
    if transport is not None: result["evidence"] = "injected_transport"
    result["signing_limit"] = "GET a_bogus; upstream x-tt-argus marker is not a full Argus SDK signature"
    return result
