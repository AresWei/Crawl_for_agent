"""Independent Reddit public listing/detail reader; no OAuth or access bypass.

Protocol: https://www.reddit.com/dev/api/ (listings, comments, morechildren).
MoreComments are retained as evidence and stay explicitly incomplete in this slice.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..core.records import Record
from ..core.sessions import open_http
from .privacy import anonymize_user_id
from .discovery_common import DiscoveryClient, DiscoveryUnavailable, schema_error, scope_key, unavailable_summary
from .weibo._shared import budgets, selections, walk

BASE = "https://www.reddit.com"
CAPABILITIES = {"modes": ["hot", "feed", "new", "top", "detail", "qa"],
    "listing_pagination": "after_fullname", "comments": "embedded_recursive_tree",
    "morechildren": "preserved_unexpanded_stubs", "qa": "comment_sort_qa_only",
    "oauth": False, "live_verified": False,
    "limits": ["Reddit academic research requires Reddit For Researchers authorization; none verified here",
               "403 blocked is not evidence login will fix access", "qa does not imply accepted/correct answers",
               "initial comment tree is a partial view; morechildren expansion not implemented"]}


def post_id(value):
    value = str(value).strip()
    if re.fullmatch(r"(?:t3_)?[a-z0-9]+", value):
        return value.removeprefix("t3_")
    parts = urlsplit(value)
    if parts.scheme in {"https", "http"} and parts.hostname in {"reddit.com", "www.reddit.com"}:
        match = re.fullmatch(r"/(?:r/[A-Za-z0-9_]+/)?comments/([a-z0-9]+)(?:/[^?#]*)?", parts.path)
        if match:
            return match[1]
    raise ValueError("Select a Reddit post ID or reddit.com comments URL")


def listing(body, evidence):
    if not isinstance(body, dict) or body.get("kind") != "Listing" or not isinstance(body.get("data"), dict):
        raise schema_error(evidence)
    data = body["data"]
    if not isinstance(data.get("children"), list) or "after" not in data:
        raise schema_error(evidence)
    after = data["after"]
    if after is not None and (not isinstance(after, str) or not re.fullmatch(r"t[13]_[a-z0-9]+", after)):
        raise schema_error(evidence)
    return data["children"], after


def normalize(thing, evidence, *, article=None, depth=0):
    if not isinstance(thing, dict) or thing.get("kind") not in {"t3", "t1"}:
        raise schema_error(evidence)
    data = thing.get("data")
    if not isinstance(data, dict) or not re.fullmatch(r"[a-z0-9]+", str(data.get("id", ""))):
        raise schema_error(evidence)
    kind, identity = thing["kind"], data["id"]
    fullname = f"{kind}_{identity}"
    if data.get("name", fullname) != fullname:
        raise schema_error(evidence)
    date = data.get("created_utc")
    try:
        source_date = datetime.fromtimestamp(float(date), timezone.utc).isoformat() if date is not None else None
    except (ValueError, OverflowError, TypeError, OSError):
        raise schema_error(evidence) from None
    payload = {"fullname": fullname, "subreddit": data.get("subreddit"),
        "creator_hash": anonymize_user_id(data.get("author")), "score": data.get("score"),
        "created_utc": date, "edited": data.get("edited"), "distinguished": data.get("distinguished")}
    if kind == "t3":
        payload.update(post_id=identity, title=data.get("title", ""), content=data.get("selftext", ""),
                       external_url=data.get("url"), num_comments=data.get("num_comments"),
                       is_self=data.get("is_self"), over_18=data.get("over_18"))
        url = f"{BASE}/comments/{identity}/"
    else:
        if not article or data.get("link_id", f"t3_{article}") != f"t3_{article}":
            raise schema_error(evidence)
        parent = data.get("parent_id")
        if not isinstance(parent, str) or not re.fullmatch(r"t[13]_[a-z0-9]+", parent):
            raise schema_error(evidence)
        payload.update(comment_id=identity, post_id=article, parent_comment_id=parent,
                       link_id=f"t3_{article}", content=data.get("body", ""), depth=depth,
                       is_submitter=data.get("is_submitter"))
        url = f"{BASE}/comments/{article}/_/{identity}/"
    return Record("reddit", "post" if kind == "t3" else "comment", identity, url, payload,
        source_date=source_date, source_date_field="created_utc", coverage={"evidence": evidence,
        "author_masked": True, "scope": "source_returned_fields", "deleted_or_removed":
        data.get("body", data.get("selftext")) in {"[deleted]", "[removed]"}})


def comment_tree(children, article, evidence):
    rows, omitted = [], []
    stack = [(thing, 0) for thing in reversed(children)]
    while stack:
        thing, depth = stack.pop()
        if not isinstance(thing, dict) or not isinstance(thing.get("data"), dict):
            raise schema_error(evidence)
        data = thing["data"]
        if thing.get("kind") == "more":
            ids = data.get("children", [])
            if not isinstance(ids, list) or any(not re.fullmatch(r"[a-z0-9]+", str(i)) for i in ids):
                raise schema_error(evidence)
            omitted.append({"id": data.get("id"), "parent_id": data.get("parent_id"),
                            "count": data.get("count"), "children": ids, "depth": depth})
            continue
        if thing.get("kind") != "t1":
            raise schema_error(evidence)
        rows.append(normalize(thing, evidence, article=article, depth=depth))
        replies = data.get("replies")
        if replies not in (None, ""):
            nested, after = listing(replies, evidence)
            if after is not None:
                omitted.append({"after": after, "parent_id": data.get("name"), "reason": "nested_cursor"})
            stack.extend((child, depth + 1) for child in reversed(nested))
    return rows, omitted


async def collect(context, *, transport=None, interval=1.0):
    params, coverage = context.params, []
    mode = params.get("mode", "hot")
    if mode not in CAPABILITIES["modes"]:
        raise ValueError("Unsupported Reddit discovery mode")
    limits = budgets(params)
    size = int(params.get("page_size", 25))
    if not 1 <= size <= 100:
        raise ValueError("page_size must be 1..100")
    http = await open_http(context, platform="reddit", account=params.get("account"), transport=transport,
        headers={"User-Agent": "ResearchCrawler/0.1 (local public research)", "Accept": "application/json"})
    client = DiscoveryClient(http, context, "reddit", interval=interval)
    try:
        if mode not in {"detail", "qa"}:
            subreddit = str(params.get("subreddit", "all"))
            if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", subreddit):
                raise ValueError("subreddit must be one community name")
            sort = "hot" if mode == "feed" else mode
            period = str(params.get("period", "day"))
            if period not in {"hour", "day", "week", "month", "year", "all"}:
                raise ValueError("Unsupported Reddit period")
            async def fetch(after):
                query = {"limit": size, "raw_json": 1, "t": period}
                if after:
                    query["after"] = after
                body, evidence = await client.get_json(f"{BASE}/r/{subreddit}/{sort}.json", query)
                children, next_after = listing(body, evidence)
                if any(not isinstance(i, dict) or i.get("kind") != "t3" for i in children):
                    raise schema_error(evidence)
                return [normalize(i, evidence) for i in children], next_after, next_after is None
            scope = scope_key([subreddit.lower(), sort, period, size])
            coverage.append(await walk(context, f"discovery:reddit:listing:{scope}", fetch, initial=None, **limits))
        else:
            ids = [post_id(value) for value in selections(params, "detail")]
            max_comments = int(params.get("max_comments", 100))
            if not 0 <= max_comments <= 1000:
                raise ValueError("max_comments must be 0..1000")
            sort = "qa" if mode == "qa" else str(params.get("comment_sort", "confidence"))
            if sort not in {"confidence", "top", "new", "controversial", "old", "random", "qa", "live"}:
                raise ValueError("Unsupported comment_sort")
            for index, article in enumerate(ids):
                if index >= limits["maximum"] or index >= limits["page_limit"]:
                    coverage.append({"complete": False, "reason": "post_or_page_limit"})
                    break
                stream = f"discovery:reddit:detail:{article}:{sort}"
                cp = await context.checkpoint(stream)
                if cp and cp["exhausted"]:
                    coverage.append(cp["metadata"]["coverage"])
                    continue
                body, evidence = await client.get_json(f"{BASE}/comments/{article}.json",
                    {"limit": min(max_comments, 500), "depth": 10, "sort": sort, "raw_json": 1})
                if not isinstance(body, list) or len(body) != 2:
                    raise schema_error(evidence)
                posts, _ = listing(body[0], evidence)
                comments, after = listing(body[1], evidence)
                if len(posts) != 1 or posts[0].get("kind") != "t3":
                    raise schema_error(evidence)
                post = normalize(posts[0], evidence)
                if post.native_id != article:
                    raise schema_error(evidence)
                rows, omitted = comment_tree(comments, article, evidence)
                # An initial tree is bounded even without more stubs: depth and
                # Reddit filtering can hide branches. Never claim full discussion.
                complete = max_comments == 0
                reason = "comments_not_requested" if complete else "comment_limit" if len(rows) > max_comments else "morechildren_unexpanded" if omitted or after else "bounded_initial_tree"
                result = {"complete": complete, "post_id": article, "reason": reason,
                    "comments_saved": min(len(rows), max_comments), "omitted_branches": omitted,
                    "after": after, "sort": sort, "max_depth": 10, "evidence": evidence}
                await context.commit_page(stream, [post] + rows[:max_comments], next_cursor=None,
                    exhausted=complete, expected_revision=cp["revision"] if cp else 0,
                    metadata={"coverage": result})
                coverage.append(result)
    except DiscoveryUnavailable as error:
        return await unavailable_summary(context, "reddit", mode, error)
    return {"status": "complete" if all(c["complete"] for c in coverage) else "partial",
        "platform": "reddit", "mode": mode, "coverage": coverage,
        "coverage_complete": all(c["complete"] for c in coverage), "qa_semantics": "source_sort_only"}
