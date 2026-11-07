# Copyright (c) 2025 relakkes@gmail.com
# Adapted from MediaCrawler media_platform/zhihu/{client,help,core}.py.
# Pinned source 54a3d92412f4b20c06b45f1d542b78dd92049d6d.
# NON-COMMERCIAL LEARNING LICENSE 1.1; licenses/mediacrawler/LICENSE.
# Changes: shared sessions, local Node signer, strict paging and atomic commits.
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode

from parsel import Selector

from ...core.jobs import NeedsLogin
from ...core.records import Record
from ..weibo._shared import (APIError, Client, anonymize_user_id, budgets,
    selections, keywords, observations, walk, summary, timestamp_date)
from .extractor import ZhihuExtractor
from .models import ZhihuContent

CAPABILITIES = {"implemented": True, "status": "source_migrated", "modes": ["search", "detail", "creator"],
    "content_types": ["answer", "article", "zvideo"], "comments": "root_cursor", "replies": "child_cursor",
    "media": [], "unsupported_upstream": ["media_download"], "creator_profiles": False,
    "dependencies": ["Node.js (builtin crypto only)", "parsel"], "live_verified": False}


def content_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("www.zhihu.com", "zhihu.com", "zhuanlan.zhihu.com"):
        raise ValueError("Select an official Zhihu answer, article or video URL")
    for kind, pattern in (("answer", r"/question/(\d+)/answer/(\d+)/?"), ("article", r"/p/(\d+)/?"), ("zvideo", r"/zvideo/(\d+)/?")):
        match = re.fullmatch(pattern, parsed.path)
        if match:
            return kind, match.groups()[-1], parsed.path.rstrip("/")
    raise ValueError("Expected answer, article or zvideo URL")


def creator_token(value):
    if re.fullmatch(r"[\w-]+", value):
        return value
    parsed = urlsplit(value)
    if parsed.hostname in ("www.zhihu.com", "zhihu.com"):
        match = re.fullmatch(r"/(?:people|org)/([\w-]+)(?:/(?:answers|posts|zvideos))?/?", parsed.path)
        if match:
            return match[1]
    raise ValueError("Expected Zhihu creator token or URL")


def normalize_content(item, keyword="", *, date_field=None):
    payload = item.model_dump()
    if not payload["content_id"] or payload["content_type"] not in ("answer", "article", "zvideo"):
        raise APIError("zhihu", "missing_content_identity")
    payload["source_keyword"] = keyword
    # API and HTML use different date aliases; preserve the actual raw field.
    return Record("zhihu", payload["content_type"], payload["content_id"], payload["content_url"], payload,
        source_date=timestamp_date(payload["created_time"]), source_date_field=date_field or "created_time",
        coverage={"author_masked": True, "media_downloaded": False})


def normalize_comment(item):
    payload = item.model_dump()
    if not payload["comment_id"]:
        raise APIError("zhihu", "missing_comment_id")
    return Record("zhihu", "comment", payload["comment_id"], "", payload,
        source_date=timestamp_date(payload["publish_time"]), source_date_field="created_time",
        coverage={"author_masked": True})


def page_cursor(body):
    paging = body.get("paging")
    if not isinstance(paging, dict) or type(paging.get("is_end")) is not bool:
        raise APIError("zhihu", "invalid_paging")
    next_cursor = parse_qs(urlsplit(paging.get("next") or "").query).get("offset", [""])[0]
    if not paging["is_end"] and not next_cursor:
        raise APIError("zhihu", "missing_next_offset")
    return next_cursor, paging["is_end"]


class ZhihuClient(Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extractor = ZhihuExtractor()
        self.node = shutil.which("node")
        self.comments_unavailable = False

    async def signed_headers(self, path):
        # Only d_c0 is needed by the local signing algorithm, never whole state.
        dc0 = next((c.value for c in self.http.cookies.jar if c.name == "d_c0" and c.domain.lstrip(".").endswith("zhihu.com")), None)
        if not dc0:
            raise NeedsLogin("zhihu", self.account, "Selected session lacks signing state")
        from ...signing.client import backend, service_sign
        from ...signing.local import zhihu_headers
        if backend(self.context) == 'service':
            return await service_sign(self.context, 'zhihu', 'headers', {'path': path, 'dc0': dc0})
        return await zhihu_headers(self.context.paths, path, dc0)

    async def get(self, path, params=None, *, html=False, comment=False):
        final = path + ("?" + urlencode(params) if params else "")
        headers = await self.signed_headers(final)
        host = "https://zhuanlan.zhihu.com" if path.startswith("/p/") else "https://www.zhihu.com"
        try:
            response = await self.request("GET", host + final, headers=headers)
        except APIError as error:
            if comment and error.code == 404:
                self.comments_unavailable = True
                return {"data": [], "paging": {"is_end": True}, "comment_endpoint_404": True}
            raise
        if html:
            return response.text
        body = self.json(response)
        if body.get("error"):
            if (body["error"] or {}).get("code") in (100, 401, 403):
                raise NeedsLogin("zhihu", self.account)
            raise APIError("zhihu", "business_error")
        return body

    def extract(self, raw):
        # Retain parser field mapping, with no additions from removed profiles.
        items = self.extractor._extract_content_list(raw)
        return [normalize_content(item, date_field=("published_at" if "video" in source else "created_at") if item.content_type == "zvideo" else "created" if item.content_type == "article" and not source.get("created_time") else "created_time") for item, source in zip(items, [r for r in raw if r.get("type") in ("answer", "article", "zvideo")])]

    async def detail(self, kind, identity, path):
        html = await self.get(path, html=True)
        text = Selector(text=html).xpath("//script[@id='js-initialData']/text()").get()
        if not text:
            raise APIError("zhihu", "missing_initial_data")
        entities = json.loads(text).get("initialState", {}).get("entities", {})
        items = entities.get({"answer": "answers", "article": "articles", "zvideo": "zvideos"}[kind]) or {}
        raw = items.get(identity)
        if not raw:
            raise APIError("zhihu", "selected_content_missing")
        raw = dict(raw, type=kind)
        if isinstance(raw.get("author"), str):
            raw["author"] = (entities.get("users") or {}).get(raw["author"], {})
        return self.extract([raw])[0]


async def collect(context, *, transport=None, interval=1.0):
    from ...core.sessions import open_http
    params, coverage = context.params, []
    mode, limits = params.get("mode", "detail"), budgets(params)
    http = await open_http(context, platform="zhihu", account=params.get("account"), transport=transport,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.zhihu.com/", "x-api-version": "3.0.91"})
    client = ZhihuClient(http, context, "zhihu", interval=interval)
    if not any(c.name == "d_c0" for c in http.cookies.jar):
        await client.request("GET", "https://www.zhihu.com/")
    if mode == "detail":
        for kind, identity, path in map(content_url, selections(params, mode)):
            async def fetch(_, kind=kind, identity=identity, path=path):
                return [await client.detail(kind, identity, path)], None, True
            coverage.append(await walk(context, f"zhihu:detail:{kind}:{identity}", fetch, **limits))
    elif mode == "search":
        for keyword in keywords(params):
            async def fetch(offset, keyword=keyword):
                body = await client.get("/api/v4/search_v3", {"gk_version": "gz-gaokao", "t": "general", "q": keyword,
                    "correction": 1, "offset": offset, "limit": 20, "filter_fields": "", "lc_idx": offset,
                    "show_all_topics": 0, "search_source": "Filter", "time_interval": params.get("search_time", ""),
                    "sort": params.get("sort", ""), "vertical": params.get("content_type", "")})
                if not isinstance(body.get("data"), list):
                    raise APIError("zhihu", "missing_search_data")
                raw = [r["object"] for r in body["data"] if r.get("type") in ("search_result", "zvideo") and r.get("object")]
                records = client.extract(raw)
                for record in records:
                    record.payload["source_keyword"] = keyword
                next_offset, ended = page_cursor(body)
                return records, next_offset, ended
            coverage.append(await walk(context, f"zhihu:search:{keyword}", fetch, initial=(int(params.get("start_page", 1)) - 1) * 20, **limits))
    elif mode == "creator":
        kinds = params.get("content_types", ["answer", "article", "zvideo"])
        if not isinstance(kinds, list) or not kinds or any(kind not in ("answer", "article", "zvideo") for kind in kinds):
            raise ValueError("content_types must select answer, article or zvideo")
        for token in map(creator_token, selections(params, mode)):
            for kind in kinds:
                async def fetch(offset, token=token, kind=kind):
                    body = await client.get(f"/api/v4/members/{token}/{kind}s", {"offset": offset, "limit": 20,
                        "order_by": "created", "include": "data[*].content,comment_count,voteup_count,created_time,updated_time,created,updated,excerpt,author,question"})
                    if not isinstance(body.get("data"), list):
                        raise APIError("zhihu", "missing_creator_data")
                    cursor, ended = page_cursor(body)
                    return client.extract(body["data"]), cursor, ended
                coverage.append(await walk(context, f"zhihu:creator:{anonymize_user_id(token)}:{kind}", fetch, initial=0, **limits))
    else:
        raise ValueError("Zhihu modes: search, detail, creator")
    maximum = int(params.get("max_comments", 0))
    if maximum:
        for row in await observations(context, "zhihu", {"answer", "article", "zvideo"}):
            content = ZhihuContent(**row["payload"])
            async def fetch(offset, content=content):
                body = await client.get(f"/api/v4/comment_v5/{content.content_type}s/{content.content_id}/root_comment", {"order": "score", "offset": offset, "limit": 10}, comment=True)
                if not isinstance(body.get("data"), list):
                    raise APIError("zhihu", "missing_comments")
                cursor, ended = page_cursor(body)
                return [normalize_comment(c) for c in client.extractor.extract_comments(content, body["data"])], cursor, ended
            coverage.append(await walk(context, f"zhihu:comments:{content.content_type}:{content.content_id}", fetch, initial="", maximum=maximum, page_limit=limits["page_limit"]))
        if params.get("include_replies"):
            for row in await observations(context, "zhihu", {"comment"}):
                item = row["payload"]
                if not item.get("sub_comment_count") or item.get("parent_comment_id") not in (None, "", "0", 0):
                    continue
                content = ZhihuContent(content_id=item["content_id"], content_type=item["content_type"])
                async def fetch(offset, root=item["comment_id"], content=content):
                    body = await client.get(f"/api/v4/comment_v5/comment/{root}/child_comment", {"order": "sort", "offset": offset, "limit": 10}, comment=True)
                    if not isinstance(body.get("data"), list):
                        raise APIError("zhihu", "missing_replies")
                    cursor, ended = page_cursor(body)
                    records = [normalize_comment(c) for c in client.extractor.extract_comments(content, body["data"])]
                    for record in records:
                        record.payload["root_comment_id"] = root
                    return records, cursor, ended
                coverage.append(await walk(context, f"zhihu:replies:{item['comment_id']}", fetch, initial="", maximum=int(params.get("max_replies", maximum)), page_limit=limits["page_limit"]))
    if client.comments_unavailable:
        coverage.append({"complete": False, "reason": "comment_endpoint_404", "meaning": "No comment payload available; absence not certified."})
    return summary("zhihu", mode, coverage)
