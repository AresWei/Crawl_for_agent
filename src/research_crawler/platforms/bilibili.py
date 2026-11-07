# Copyright (c) 2025 relakkes@gmail.com
# Adapted from MediaCrawler, NON-COMMERCIAL LEARNING LICENSE 1.1.
# https://github.com/NanmiCoder/MediaCrawler/tree/54a3d92412f4b20c06b45f1d542b78dd92049d6d
# Sources: media_platform/bilibili/{client,help}.py, store/bilibili/__init__.py,
# tools/user_hash.py. Full license: licenses/mediacrawler/LICENSE.
# Changes: injected HTTP lifecycle, pure normalizers, typed failures, strict
# pagination validation, bounded collection, and shared atomic checkpoints.
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urlsplit

import httpx

from ..core.jobs import JobContext, NeedsLogin
from ..core.records import Record
from .privacy import anonymize_user_id, mask_nickname

CAPABILITIES = {"implemented": True, "status": "source_migrated",
    "modes": ["detail", "search", "creator", "dynamics"], "comments": "root_cursor", "replies": "page_number",
    "search_modes": ["normal", "time_range", "daily_limit"], "creator_profiles": False,
    "media": ["cover", "dash_video_audio", "durl_segments"], "live_verified": False,
    "limitations": ["Video quality remains account/entitlement dependent.", "Durl segments require verified assembly by the shared media manager; missing segments remain partial."]}


class PlatformAPIError(RuntimeError):
    """Safe error marker; never includes response bodies or session data."""

    def __init__(self, code: int | str):
        self.code = code
        super().__init__(f"Bilibili API failure ({code})")


class BilibiliSign:
    """MediaCrawler's WBI algorithm with a non-mutating, injectable clock."""

    MAP = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
           5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
           24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
           6, 63, 57, 62, 11, 36, 20, 34, 44, 52)

    def __init__(self, img_key: str, sub_key: str):
        combined = img_key + sub_key
        if len(combined) < 64:
            raise PlatformAPIError("invalid_wbi_keys")
        self.salt = "".join(combined[index] for index in self.MAP)[:32]

    def sign(self, params: dict, timestamp: int | None = None) -> dict:
        values = dict(params, wts=int(time.time()) if timestamp is None else timestamp)
        values = {key: "".join(ch for ch in str(value) if ch not in "!'()*")
                  for key, value in sorted(values.items())}
        values["w_rid"] = hashlib.md5((urlencode(values) + self.salt).encode()).hexdigest()
        return values


def video_id(value: str) -> str:
    """Accept selected BV IDs or official full URLs; never fetch arbitrary URLs."""
    value = str(value).strip()
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname in {"www.bilibili.com", "bilibili.com"}:
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", parsed.path)
        if match:
            return match[1]
    raise ValueError("Expected a BV identifier or a full bilibili.com/video/BV URL")


def source_date(value) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()


def normalize_video(data: dict) -> Record:
    view = data.get("View")
    if not isinstance(view, dict) or not view.get("aid"):
        raise PlatformAPIError("missing_video")
    owner, stat = view.get("owner") or {}, view.get("stat") or {}
    aid = str(view["aid"])
    payload = {
        "video_id": aid, "bvid": view.get("bvid", ""), "video_type": "video",
        "title": str(view.get("title", ""))[:500], "desc": str(view.get("desc", ""))[:500],
        "create_time": view.get("pubdate"), "creator_hash": anonymize_user_id(owner.get("mid")),
        "nickname": mask_nickname(owner.get("name")),
        "video_url": f"https://www.bilibili.com/video/av{aid}",
        "video_cover_url": view.get("pic", ""), "source_keyword": "",
    }
    for output, field in (("liked_count", "like"), ("disliked_count", "dislike"),
                          ("video_play_count", "view"), ("video_favorite_count", "favorite"),
                          ("video_share_count", "share"), ("video_coin_count", "coin"),
                          ("video_danmaku", "danmaku"), ("video_comment", "reply")):
        payload[output] = str(stat.get(field, ""))
    return Record("bili", "video", aid, payload["video_url"], payload,
                  source_date=source_date(view.get("pubdate")), source_date_field="View.pubdate",
                  coverage={"endpoint": "/x/web-interface/view/detail", "description_limit": 500,
                            "description_truncated": len(str(view.get("desc", ""))) > 500,
                            "author_masked": True, "media_downloaded": False})


def normalize_comment(aid: str, item: dict) -> Record:
    if item.get("rpid") is None:
        raise PlatformAPIError("missing_comment_id")
    member = item.get("member") or {}
    payload = {
        "comment_id": str(item["rpid"]), "parent_comment_id": str(item.get("parent", 0)),
        "create_time": item.get("ctime"), "video_id": str(aid),
        "content": (item.get("content") or {}).get("message", ""),
        "creator_hash": anonymize_user_id(member.get("mid")), "nickname": mask_nickname(member.get("uname")),
        "sub_comment_count": str(item.get("rcount", 0)), "like_count": item.get("like", 0),
    }
    return Record("bili", "comment", payload["comment_id"],
                  f"https://www.bilibili.com/video/av{aid}#reply{item['rpid']}", payload,
                  source_date=source_date(item.get("ctime")), source_date_field="ctime",
                  coverage={"endpoint": "/x/v2/reply/wbi/main", "author_masked": True,
                            "reply_collection": "not_requested_or_not_implemented"})


def extract_pinned(value) -> list[dict]:
    # Recursive first-page top/top_replies handling retained from client.py.
    if isinstance(value, dict):
        if value.get("rpid") is not None:
            return [value]
        return [item for child in value.values() for item in extract_pinned(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in extract_pinned(child)]
    return []


class BilibiliClient:
    def __init__(self, http: httpx.AsyncClient, context: JobContext, *, account: str = "anonymous", interval: float = 1.0):
        self.http, self.context, self.account, self.interval = http, context, account, interval
        self.signer = None
        self.last_request = 0.0

    async def get(self, path: str, params: dict | None = None, *, signed: bool = False, nav_keys: bool = False) -> dict:
        await self.context.check_control()
        if signed:
            if self.signer is None:
                nav = await self.get("/x/web-interface/nav", nav_keys=True)
                images = nav.get("wbi_img") or {}
                keys = [urlsplit(images.get(name, "")).path.rsplit("/", 1)[-1].split(".")[0]
                        for name in ("img_url", "sub_url")]
                self.signer = BilibiliSign(*keys)
            params = self.signer.sign(params or {})
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        await self.context.check_control()
        self.last_request = time.monotonic()
        response = await self.http.get("https://api.bilibili.com" + path, params=params)
        if response.status_code == 401:
            raise NeedsLogin("bili", self.account)
        if response.status_code != 200:
            raise PlatformAPIError(response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise PlatformAPIError("invalid_json") from None
        if not isinstance(body, dict):
            raise PlatformAPIError("invalid_envelope")
        data, code = body.get("data"), body.get("code")
        # An anonymous nav response may carry usable public WBI keys with -101.
        keys_available = nav_keys and code == -101 and isinstance(data, dict) and data.get("wbi_img")
        if code in {-101, -111} and not keys_available:
            raise NeedsLogin("bili", self.account)
        if code != 0 and not keys_available:
            raise PlatformAPIError(code if isinstance(code, int) else "missing_code")
        if not isinstance(data, dict):
            raise PlatformAPIError("missing_data")
        return data

    async def detail(self, bvid: str = "", *, aid: str = "") -> dict:
        return await self.get("/x/web-interface/view/detail", {"aid": aid} if aid else {"bvid": bvid})

    async def comments(self, aid: str, cursor: int) -> dict:
        return await self.get("/x/v2/reply/wbi/main", {"oid": aid, "mode": 0, "type": 1, "ps": 20, "next": cursor}, signed=True)

    async def replies(self, aid: str, root: str, page: int) -> dict:
        return await self.get("/x/v2/reply/reply", {"oid": aid, "mode": 0, "type": 1, "ps": 20, "pn": page, "root": root}, signed=True)

    async def search(self, keyword: str, page: int, *, order="", begin=0, end=0):
        return await self.get("/x/web-interface/wbi/search/type", {"search_type": "video", "keyword": keyword,
            "page": page, "page_size": 20, "order": order, "pubtime_begin_s": begin, "pubtime_end_s": end}, signed=True)

    async def creator(self, identity: str, page: int, order="pubdate"):
        return await self.get("/x/space/wbi/arc/search", {"mid": identity, "pn": page, "ps": 30, "order": order}, signed=True)

    async def dynamics(self, identity: str, offset=""):
        return await self.get("/x/polymer/web-dynamic/v1/feed/space", {"offset": offset, "host_mid": identity, "platform": "web"}, signed=True)

    async def play(self, aid, cid, quality=80, fnval=4048):
        return await self.get("/x/player/wbi/playurl", {"avid": aid, "cid": cid, "qn": quality, "fourk": 1,
            "fnval": fnval, "platform": "pc"}, signed=True)


async def collect_comments(context: JobContext, client: BilibiliClient, aid: str, *, maximum: int, page_limit: int) -> dict:
    stream = f"bili:comments:{aid}"
    cp = await context.checkpoint(stream)
    seen = set((cp or {}).get("metadata", {}).get("seen_ids", []))
    cursors = set((cp or {}).get("metadata", {}).get("seen_cursors", []))
    pages = 0
    if cp and cp["exhausted"]:
        return {"complete": True, "count": len(seen), "reason": "end"}
    cursor = cp["cursor"] if cp else 0
    while len(seen) < maximum and pages < page_limit:
        data = await client.comments(aid, cursor)
        info = data.get("cursor")
        if not isinstance(info, dict) or type(info.get("is_end")) is not bool or type(info.get("next")) is not int:
            raise PlatformAPIError("invalid_comment_cursor")
        replies = data.get("replies") or []
        if not isinstance(replies, list):
            raise PlatformAPIError("invalid_comments")
        items = (extract_pinned((data.get("top"), data.get("top_replies"))) if cursor == 0 else []) + replies
        batch, truncated = [], False
        for item in items:
            if not isinstance(item, dict) or item.get("rpid") is None:
                raise PlatformAPIError("invalid_comment")
            identity = str(item["rpid"])
            if identity in seen:
                continue
            if len(seen) >= maximum:
                truncated = True
                break
            batch.append(normalize_comment(aid, item))
            seen.add(identity)
        next_cursor = info["next"]
        exhausted = info["is_end"] and not truncated
        stalled = not exhausted and not truncated and (next_cursor == cursor or next_cursor in cursors)
        cursors.add(cursor)
        reason = "end" if exhausted else "comment_limit" if truncated else "repeated_cursor" if stalled else "next_page"
        cp = await context.commit_page(stream, batch, next_cursor=cursor if truncated or stalled else next_cursor,
            exhausted=exhausted, expected_revision=cp["revision"] if cp else 0,
            metadata={"seen_ids": sorted(seen), "seen_cursors": sorted(cursors), "reason": reason,
                      "root_comments_only": True})
        pages += 1
        if exhausted or stalled or truncated:
            return {"complete": exhausted, "count": len(seen), "reason": reason}
        cursor = next_cursor
    return {"complete": False, "count": len(seen), "reason": "comment_limit" if len(seen) >= maximum else "page_limit"}


def creator_id(value):
    if value.isdigit():
        return value
    url = urlsplit(value)
    if url.hostname == "space.bilibili.com" and re.fullmatch(r"/\d+/?", url.path):
        return url.path.strip("/")
    raise ValueError("Expected numeric creator ID or space.bilibili.com URL")


def normalize_dynamic(item):
    identity = str(item.get("id_str") or "")
    if not identity:
        raise PlatformAPIError("missing_dynamic_id")
    modules = item.get("modules") or {}
    author, dynamic, stats = modules.get("module_author") or {}, modules.get("module_dynamic") or {}, modules.get("module_stat") or {}
    payload = {"dynamic_id": identity, "creator_hash": anonymize_user_id(author.get("mid")),
        "user_name": mask_nickname(author.get("name")), "text": (dynamic.get("desc") or {}).get("text", ""),
        "type": str(item.get("type", "")).split("_")[-1], "pub_ts": author.get("pub_ts"),
        "total_comments": (stats.get("comment") or {}).get("count", 0),
        "total_forwards": (stats.get("forward") or {}).get("count", 0),
        "total_liked": (stats.get("like") or {}).get("count", 0)}
    return Record("bili", "dynamic", identity, f"https://t.bilibili.com/{identity}", payload,
        source_date=source_date(author.get("pub_ts")), source_date_field="modules.module_author.pub_ts",
        coverage={"author_masked": True})


def media_candidates(view, play=None, quality=80):
    """Retain upstream DASH quality/codec preference and durl segment warning."""
    media = [{"kind": "image", "role": "cover", "url": view["pic"]}] if view.get("pic") else []
    if not play:
        return media
    dash = play.get("dash") if isinstance(play.get("dash"), dict) else {}
    stream_url = lambda s: s.get("base_url") or s.get("baseUrl") or ""
    videos = [s for s in dash.get("video") or [] if isinstance(s, dict) and stream_url(s)]
    audios = [s for s in dash.get("audio") or [] if isinstance(s, dict) and stream_url(s)]
    if videos and audios:
        priority = {7: 0, 13: 1, 12: 2}
        videos.sort(key=lambda s: (int(s.get("id") or 0), -priority.get(int(s.get("codecid") or 0), 3), int(s.get("bandwidth") or 0)))
        within = [s for s in videos if int(s.get("id") or 0) <= quality]
        video = within[-1] if within else [s for s in videos if s.get("id") == videos[0].get("id")][-1]
        audio = max(audios, key=lambda s: (int(s.get("id") or 0), int(s.get("bandwidth") or 0)))
        media.append({"kind": "video", "url": stream_url(video), "backup_urls": video.get("backup_url") or video.get("backupUrl") or [],
            "audio_url": stream_url(audio), "audio_backup_urls": audio.get("backup_url") or audio.get("backupUrl") or [],
            "quality": video.get("id"), "available_qualities": sorted({int(s.get("id") or 0) for s in videos}, reverse=True),
            "requires_mux": True, "format": "dash", "complete": True})
    else:
        segments = play.get("durl") or []
        for index, segment in enumerate(segments, 1):
            if not isinstance(segment, dict) or not segment.get("url"):
                continue
            media.append({"kind": "video", "url": segment["url"], "backup_urls": segment.get("backup_url") or [],
                "format": "durl", "segment_index": index, "segment_count": len(segments), "complete": len(segments) == 1,
                "requires_concat": len(segments) > 1})
    return media


def search_windows(params):
    mode = params.get("search_mode", "normal")
    if mode == "normal":
        return [("all", 0, 0)]
    if mode not in ("time_range", "daily_limit"):
        raise ValueError("Invalid Bilibili search_mode")
    start = datetime.strptime(params["start_day"], "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
    end = datetime.strptime(params["end_day"], "%Y-%m-%d").replace(tzinfo=start.tzinfo)
    if end < start or (end - start).days > 366:
        raise ValueError("Choose a date range no longer than 367 days")
    return [((day := start + timedelta(days=i)).strftime("%Y-%m-%d"), int(day.timestamp()), int((day + timedelta(days=1)).timestamp()) - 1)
            for i in range((end - start).days + 1)]


async def collect(context, *, transport=None, interval=1.0):
    from ..core.sessions import open_http
    from .weibo._shared import budgets, selections, keywords, observations, walk, bounded_items
    params, mode, coverage = context.params, context.params.get("mode", "detail"), []
    limits = budgets(params)
    http = await open_http(context, platform="bili", account=params.get("account"), transport=transport,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
    client = BilibiliClient(http, context, account=params.get("account") or "anonymous", interval=interval)
    async def detail(bvid="", aid="", keyword=""):
        data = await client.detail(bvid, aid=aid)
        record = normalize_video(data)
        record.payload["source_keyword"] = keyword
        view, play = data["View"], None
        if params.get("include_media") or params.get("download_media"):
            if not view.get("cid"):
                record.coverage["media_reason"] = "missing_cid"
            else:
                play = await client.play(record.native_id, view["cid"], int(params.get("quality", 80)))
                if not (play.get("dash") or {}).get("video") and not play.get("durl"):
                    play = await client.play(record.native_id, view["cid"], int(params.get("quality", 80)), fnval=1)
        record.payload["media"] = media_candidates(view, play, int(params.get("quality", 80)))
        record.coverage["media_requested"] = bool(params.get("include_media") or params.get("download_media"))
        return record
    if mode == "detail":
        for bvid in map(video_id, selections(params, mode)):
            stream = f"bili:detail:{bvid}"
            cp = await context.checkpoint(stream)
            if cp and cp["exhausted"]:
                aid = cp["metadata"]["aid"]
            else:
                record = await detail(bvid)
                aid = record.native_id
                await context.commit_page(stream, [record], next_cursor=None, exhausted=True,
                    expected_revision=cp["revision"] if cp else 0, metadata={"aid": aid, "bvid": bvid})
            coverage.append({"bvid": bvid, "detail": True, "complete": True})
    elif mode == "search":
        windows = search_windows(params)
        for keyword in keywords(params):
            consumed = 0
            for label, begin, end in windows:
                if consumed >= limits["maximum"]:
                    coverage.append({"complete": False, "reason": "record_limit", "window": label})
                    break
                async def fetch(page, keyword=keyword, begin=begin, end=end):
                    body = await client.search(keyword, page, order=params.get("sort", ""), begin=begin, end=end)
                    if not isinstance(body.get("result"), list):
                        raise PlatformAPIError("missing_search_results")
                    selected, truncated = bounded_items(fetch, body["result"], "bili", "video", lambda item: item.get("aid"))
                    records = [await detail(item.get("bvid", ""), str(item.get("aid") or ""), keyword) for item in selected]
                    ended = not body["result"] or (isinstance(body.get("numPages"), int) and page >= body["numPages"])
                    return records, page + 1, ended, truncated
                daily = int(params.get("daily_max", params.get("max_notes_per_day", limits["maximum"])))
                if daily < 1:
                    raise ValueError("daily_max must be positive")
                result = await walk(context, f"bili:search:{keyword}:{label}", fetch, initial=int(params.get("start_page", 1)),
                    maximum=min(limits["maximum"] - consumed, daily) if label != "all" else limits["maximum"], page_limit=limits["page_limit"])
                consumed += result["count"]
                coverage.append(dict(result, window=label))
    elif mode in ("creator", "dynamics"):
        for identity in map(creator_id, selections(params, "creator")):
            if mode == "creator" and params.get("creator_mode", "videos") != "dynamics":
                async def fetch(page, identity=identity):
                    body = await client.creator(identity, page, params.get("sort", "pubdate"))
                    items, info = (body.get("list") or {}).get("vlist"), body.get("page") or {}
                    if not isinstance(items, list) or "count" not in info:
                        raise PlatformAPIError("invalid_creator_page")
                    selected, truncated = bounded_items(fetch, items, "bili", "video", lambda item: item.get("aid"))
                    return [await detail(item.get("bvid", ""), str(item.get("aid") or "")) for item in selected], page + 1, page * 30 >= int(info["count"]), truncated
                coverage.append(await walk(context, f"bili:creator:{anonymize_user_id(identity)}", fetch, **limits))
            if mode == "dynamics" or params.get("creator_mode") == "dynamics" or params.get("include_dynamics"):
                async def fetch(offset, identity=identity):
                    body = await client.dynamics(identity, offset)
                    if not isinstance(body.get("items"), list) or "has_more" not in body or "offset" not in body:
                        raise PlatformAPIError("invalid_dynamic_page")
                    return [normalize_dynamic(item) for item in body["items"]], body["offset"], not bool(body["has_more"])
                coverage.append(await walk(context, f"bili:dynamics:{anonymize_user_id(identity)}", fetch, initial="", **limits))
    else:
        raise ValueError("Bilibili modes: search, detail, creator, dynamics")
    maximum = int(params.get("max_comments", 0))
    videos = await observations(context, "bili", {"video"})
    for row in videos:
        comments = await collect_comments(context, client, row["native_id"], maximum=maximum, page_limit=limits["page_limit"]) if maximum else {"complete": True, "count": 0, "reason": "not_requested"}
        if mode == "detail":
            match = next((r for r in coverage if r.get("bvid") == row["payload"].get("bvid")), None)
            if match is not None:
                match.update(comments=comments, complete=comments["complete"])
        else:
            coverage.append(comments)
    if maximum and params.get("include_replies"):
        for row in await observations(context, "bili", {"comment"}):
            item = row["payload"]
            if item.get("parent_comment_id") != "0" or not int(item.get("sub_comment_count") or 0):
                continue
            async def fetch(page, item=item):
                body = await client.replies(item["video_id"], item["comment_id"], page)
                info = body.get("page") or {}
                if "count" not in info or not isinstance(body.get("replies", []), list):
                    raise PlatformAPIError("invalid_reply_page")
                records = [normalize_comment(item["video_id"], child) for child in body.get("replies") or []]
                for record in records:
                    record.coverage.update(endpoint="/x/v2/reply/reply", reply_collection="paginated")
                    record.payload["root_comment_id"] = item["comment_id"]
                return records, page + 1, page * int(info.get("size", 20)) >= int(info["count"])
            coverage.append(await walk(context, f"bili:replies:{item['video_id']}:{item['comment_id']}", fetch,
                maximum=int(params.get("max_replies", maximum)), page_limit=limits["page_limit"]))
    return {"status": "complete" if all(row.get("complete", False) for row in coverage) else "partial",
        "platform": "bili", "mode": mode, "coverage": coverage, "creator_profiles_persisted": False,
        "scope": "bounded_source_supported_collection", "replies_collected": bool(maximum and params.get("include_replies"))}
