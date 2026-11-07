# Copyright (c) 2025 relakkes@gmail.com
# Adapted from MediaCrawler media_platform/tieba/{client,help,core}.py.
# Source: 54a3d92412f4b20c06b45f1d542b78dd92049d6d.
# NON-COMMERCIAL LEARNING LICENSE 1.1; licenses/mediacrawler/LICENSE.
# Changes: HTTP-first requests, shared browser fallback, atomic page checkpoints.
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, unquote, quote, urlencode, urlsplit

import httpx

from ...core.jobs import NeedsLogin
from ...core.records import Record
from ..weibo._shared import (APIError, Client, anonymize_user_id, budgets,
    selections, keywords, observations, walk, summary, bounded_items)
from .extractor import TieBaExtractor
from .models import TiebaNote, TiebaComment

CAPABILITIES = {"implemented": True, "status": "source_migrated", "modes": ["search", "detail", "creator", "forum"],
    "comments": "thread_page", "replies": "subcomment_html_page", "media": [],
    "unsupported_upstream": ["media_download"], "creator_profiles": False, "live_verified": False,
    "browser_fallback": "shared_task_session", "dependencies": ["parsel"]}


def sign_pc_params(params):
    source = "".join(f"{key}={params[key]}" for key in sorted(params) if key not in {"sign", "sig"} and params[key] is not None)
    return hashlib.md5((source + "36770b1f34c9bbf2e7d1a99d2b82fa9e").encode()).hexdigest()


def note_id(value):
    if value.isdigit():
        return value
    url = urlsplit(value)
    if url.hostname == "tieba.baidu.com":
        match = re.fullmatch(r"/p/(\d+)/?", url.path)
        if match:
            return match[1]
    raise ValueError("Expected Tieba numeric thread ID or official /p/ URL")


def creator_route(value):
    parsed = urlsplit(value)
    if parsed.hostname:
        if parsed.hostname != "tieba.baidu.com" or parsed.path.rstrip("/") != "/home/main":
            raise ValueError("Expected official Tieba creator URL")
        values = parse_qs(parsed.query)
        if values.get("id"):
            return "portrait", values["id"][0]
        if values.get("un"):
            return "username", values["un"][0]
        raise ValueError("Creator URL needs id or un")
    if not value or "/" in value or "?" in value:
        raise ValueError("Expected creator portrait or URL")
    return "portrait", value


def normalize(item, *, keyword="", endpoint="page_pc", date_field=None):
    payload = item.model_dump()
    is_comment = isinstance(item, TiebaComment)
    identity = payload["comment_id" if is_comment else "note_id"]
    if not identity:
        raise APIError("tieba", "missing_record_id")
    if not is_comment:
        payload["source_keyword"] = keyword
    raw = payload.get("publish_time", "")
    try:
        date = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat() if raw and not raw.startswith("1970-") else None
    except ValueError:
        date = None
    return Record("tieba", "comment" if is_comment else "note", identity, payload["note_url"], payload,
        source_date=date, source_date_field=date_field or ("post_list[].time" if is_comment else "first_floor.time"),
        coverage={"endpoint": endpoint, "author_masked": True, "source_time_unparsed": bool(raw and not date)})


class TiebaClient(Client):
    def __init__(self, *args, browser_fallback=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.extractor, self.tbs, self.browser = TieBaExtractor(), None, None
        self.browser_fallback = browser_fallback

    async def browser_fetch(self, path, method="GET", params=None, data=None):
        from ...core.sessions import open_browser
        if self.browser is None:
            self.browser = await open_browser(self.context, platform="tieba", account=self.context.params.get("account"),
                headless=bool(self.context.params.get("headless", True)))
            await self.browser.page.goto("https://tieba.baidu.com/", wait_until="domcontentloaded")
        await self.throttle()
        result = await self.browser.page.evaluate("""async ({url, method, body}) => {
            const options={method,credentials:'include',headers:{Accept:'application/json, text/plain, */*'}};
            if(method==='POST'){options.headers['Content-Type']='application/x-www-form-urlencoded;charset=UTF-8';options.body=body;}
            const response=await fetch(url,options);return {status:response.status,text:await response.text()};
        }""", {"url": "https://tieba.baidu.com" + path + ("?" + urlencode(params) if params else ""),
               "method": method, "body": urlencode(data or {})})
        if result["status"] in (401, 403):
            raise NeedsLogin("tieba", self.account)
        if result["status"] != 200:
            raise APIError("tieba", result["status"])
        return result["text"]

    async def get(self, path, params=None, *, method="GET", data=None, signed=False, html=False):
        params, data = dict(params or {}), dict(data or {})
        if signed:
            target = data if method == "POST" else params
            target.setdefault("subapp_type", "pc")
            target.setdefault("_client_type", "20")
            target["sign"] = sign_pc_params(target)
        try:
            response = await self.request(method, "https://tieba.baidu.com" + path, params=params, data=data if method == "POST" else None)
            text = response.text
            if not html:
                json.loads(text)
        except (APIError, NeedsLogin, httpx.RequestError, ValueError):
            if not self.browser_fallback:
                raise
            text = await self.browser_fetch(path, method, params, data)
        if html:
            if "百度安全验证" in text or "wappass.baidu.com" in text:
                raise NeedsLogin("tieba", self.account)
            return text
        try:
            body = json.loads(text)
        except ValueError:
            raise APIError("tieba", "invalid_json") from None
        if not isinstance(body, dict):
            raise APIError("tieba", "invalid_envelope")
        code = body.get("error_code", body.get("no", 0))
        if str(code) not in ("0", "None"):
            if str(code) in ("1", "4", "110001", "110004"):
                raise NeedsLogin("tieba", self.account)
            raise APIError("tieba", "business_error")
        return body

    async def sync(self):
        if self.tbs is None:
            body = await self.get("/c/s/pc/sync", signed=True)
            self.tbs = ((body.get("data") or {}).get("anti") or {}).get("tbs")
            if not self.tbs:
                raise NeedsLogin("tieba", self.account, "Selected session has no PC API state")
        return self.tbs

    async def page(self, identity, page=1):
        return await self.get("/c/f/pb/page_pc", method="POST", data={"pn": page, "lz": 0, "r": 2,
            "mark_type": 0, "back": 0, "fr": "", "kz": identity, "session_request_times": 1, "tbs": await self.sync()}, signed=True)

    async def detail(self, identity, keyword=""):
        data = await self.page(identity)
        if not isinstance(data.get("thread"), dict):
            raise APIError("tieba", "missing_thread")
        note = self.extractor.extract_note_detail_from_api(data)
        if note.note_id != identity:
            raise APIError("tieba", "thread_identity_mismatch")
        return normalize(note, keyword=keyword, date_field="first_floor.time" if (data.get("first_floor") or {}).get("time") else "thread.create_time")


async def collect(context, *, transport=None, interval=1.0):
    from ...core.sessions import open_http
    params, coverage = context.params, []
    mode, limits = params.get("mode", "detail"), budgets(params)
    http = await open_http(context, platform="tieba", account=params.get("account"), transport=transport,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://tieba.baidu.com/"})
    client = TiebaClient(http, context, "tieba", interval=interval, browser_fallback=transport is None and params.get("browser_fallback", True))
    if mode == "detail":
        for identity in map(note_id, selections(params, mode)):
            async def fetch(_, identity=identity):
                return [await client.detail(identity)], None, True
            coverage.append(await walk(context, f"tieba:detail:{identity}", fetch, **limits))
    elif mode == "search":
        for keyword in keywords(params):
            async def fetch(page, keyword=keyword):
                body = await client.get("/mo/q/search/multsearch", {"rn": 20, "st": params.get("sort", "1"),
                    "word": keyword, "needbrand": 1, "sug_type": 2, "pn": page, "come_from": "search"}, signed=True)
                if not isinstance((body.get("data") or {}).get("card_list"), list):
                    raise APIError("tieba", "missing_search_cards")
                notes = client.extractor.extract_search_note_list_from_api(body)
                # Source orchestration fetches full thread details for search hits.
                selected, truncated = bounded_items(fetch, notes, "tieba", "note", lambda note: note.note_id)
                return [await client.detail(note.note_id, keyword) for note in selected], page + 1, not notes, truncated
            coverage.append(await walk(context, f"tieba:search:{keyword}", fetch, initial=int(params.get("start_page", 1)), **limits))
    elif mode == "forum":
        forums = params.get("forums", params.get("forum_names", []))
        if not isinstance(forums, list) or not forums or len(forums) > 20:
            raise ValueError("Select 1..20 forum names")
        for forum in forums:
            async def fetch(page, forum=forum):
                body = await client.get("/c/f/frs/page_pc", method="POST", signed=True, data={"kw": quote(forum),
                    "pn": page, "sort_type": -1, "is_newfrs": 1, "is_newfeed": 1, "rn": 30, "rn_need": 10, "tbs": await client.sync()})
                if not isinstance(body.get("forum"), dict):
                    raise APIError("tieba", "missing_forum")
                notes = client.extractor.extract_tieba_note_list_from_frs_api(body)
                selected, truncated = bounded_items(fetch, notes, "tieba", "note", lambda note: note.note_id)
                return [await client.detail(note.note_id) for note in selected], page + 1, not notes or (body.get("page") or {}).get("has_more") == 0, truncated
            coverage.append(await walk(context, f"tieba:forum:{forum}", fetch, initial=int(params.get("start_page", 1)), **limits))
    elif mode == "creator":
        for route, value in map(creator_route, selections(params, mode)):
            async def fetch(page, route=route, value=value):
                if route == "portrait":
                    body = await client.get("/c/u/feed/myThread", {"pn": page, "rn": 20, "portrait": value, "type": 1, "un": ""}, signed=True)
                    data = body.get("data") or {}
                    if not isinstance(data.get("list"), list) or "has_more" not in data:
                        raise APIError("tieba", "invalid_creator_page")
                    ids = client.extractor.extract_creator_thread_id_list_from_api(body)
                    ended = not bool(int(data["has_more"]))
                else:
                    body = await client.get("/home/get/getthread", {"un": value, "pn": page, "id": "utf-8"})
                    data = body.get("data") or {}
                    content = data.get("content")
                    if not isinstance(content, str):
                        raise APIError("tieba", "invalid_creator_html")
                    ids = client.extractor.extract_tieba_thread_id_list_from_creator_page(content)
                    ended = not ids
                selected, truncated = bounded_items(fetch, ids, "tieba", "note", lambda identity: identity)
                return [await client.detail(identity) for identity in selected], page + 1, ended, truncated
            coverage.append(await walk(context, f"tieba:creator:{anonymize_user_id(value)}", fetch, initial=int(params.get("start_page", 1)), **limits))
    else:
        raise ValueError("Tieba modes: search, detail, creator, forum")
    maximum = int(params.get("max_comments", 0))
    if maximum:
        for row in await observations(context, "tieba", {"note"}):
            note = TiebaNote(**row["payload"])
            async def fetch(page, note=note):
                body = await client.page(note.note_id, page)
                info = body.get("page")
                if not isinstance(body.get("post_list"), list) or not isinstance(info, dict) or not isinstance(info.get("total_page"), int):
                    raise APIError("tieba", "invalid_comment_page")
                comments = client.extractor.extract_tieba_note_parent_comments_from_api(body, note)
                return [normalize(c) for c in comments], page + 1, page >= info["total_page"]
            coverage.append(await walk(context, f"tieba:comments:{note.note_id}", fetch, maximum=maximum, page_limit=limits["page_limit"]))
        if params.get("include_replies"):
            for row in await observations(context, "tieba", {"comment"}):
                parent = TiebaComment(**row["payload"])
                if parent.parent_comment_id or not parent.sub_comment_count:
                    continue
                async def fetch(page, parent=parent):
                    text = await client.get("/p/comment", {"tid": parent.note_id, "pid": parent.comment_id,
                        "fid": parent.tieba_id, "pn": page}, html=True)
                    comments = client.extractor.extract_tieba_note_sub_comments(text, parent)
                    if not comments and page <= (parent.sub_comment_count + 9) // 10:
                        raise APIError("tieba", "missing_expected_reply_page")
                    return [normalize(c, endpoint="/p/comment", date_field="span.lzl_time (Asia/Shanghai)") for c in comments], page + 1, page * 10 >= parent.sub_comment_count
                coverage.append(await walk(context, f"tieba:replies:{parent.comment_id}", fetch, maximum=int(params.get("max_replies", maximum)), page_limit=limits["page_limit"]))
    return summary("tieba", mode, coverage, browser_used=client.browser is not None)
