"""Independent Tieba hot-topic snapshot, based on the public observed schema.

Topics are not posts. No pagination or personalized recommendation protocol is
inferred from this single public listing. Shared sessions own all networking.
"""
from __future__ import annotations

from html import unescape
from urllib.parse import parse_qs, urlsplit

from ..core.records import Record, canonical_json
from ..core.sessions import open_http
from .discovery_common import DiscoveryClient, DiscoveryUnavailable, schema_error, unavailable_summary
from .weibo._shared import budgets

ENDPOINT = "https://tieba.baidu.com/hottopic/browse/topicList"
STREAM = "discovery:tieba:hot_topics:snapshot:v1"
CAPABILITIES = {
    "modes": ["hot", "feed"], "feed_kind": "public_hot_topics_snapshot",
    "pagination": "none; one observed snapshot", "personalized": False,
    "record_kind": "topic", "comments": False, "media": ["topic_image_urls"],
    "live_verified": "anonymous_endpoint_schema_2026-11-07",
    "limits": ["feed is an alias of hot", "topics are not posts",
               "resume reuses the original snapshot; new job refreshes",
               "public undocumented web endpoint; access may change"],
}


def _snapshot(body, evidence, maximum_bytes):
    if not isinstance(body, dict):
        raise schema_error(evidence)
    if body.get("errno") != 0:
        # Unknown application error codes do not establish login requirements.
        raise DiscoveryUnavailable("api_error", dict(evidence, api_code=body.get("errno")))
    data = body.get("data")
    group = data.get("bang_topic") if isinstance(data, dict) else None
    topics = group.get("topic_list") if isinstance(group, dict) else None
    if not isinstance(topics, list):
        raise schema_error(evidence)
    if len(topics) > 1000:
        raise schema_error(evidence, "snapshot_item_limit")
    seen = set()
    for topic in topics:
        if not isinstance(topic, dict):
            raise schema_error(evidence)
        identity = topic.get("topic_id")
        if (type(identity) not in (str, int) or not str(identity).isascii()
                or not str(identity).isdigit() or int(identity) <= 0
                or str(identity) in seen or not isinstance(topic.get("topic_name"), str)):
            raise schema_error(evidence)
        seen.add(str(identity))
    snapshot = {"topics": topics, "provider_timestamp_raw": data.get("timestamp"),
                "evidence": evidence}
    if len(canonical_json(snapshot).encode("utf-8")) > maximum_bytes:
        raise schema_error(evidence, "snapshot_byte_limit")
    return snapshot


def _record(topic, position, snapshot):
    identity = str(topic["topic_id"])
    detail_url = unescape(str(topic.get("topic_url") or ""))
    try:
        parsed = urlsplit(detail_url)
        verified_shape = (parsed.scheme == "https" and parsed.netloc == "tieba.baidu.com"
            and parsed.path == "/hottopic/browse/hottopic"
            and parse_qs(parsed.query).get("topic_id") == [identity]
            and not any(ord(char) < 32 for char in detail_url))
    except ValueError:
        verified_shape = False
    return Record("tieba", "topic", identity, detail_url if verified_shape else ENDPOINT,
        {"title": topic["topic_name"], "raw_topic": topic,
         "provider_timestamp_raw": snapshot["provider_timestamp_raw"]},
        observed_at=snapshot["evidence"]["observed_at"],
        coverage={"endpoint": ENDPOINT, "feed_kind": CAPABILITIES["feed_kind"],
            "detail_url_unverified": not verified_shape,
            "url_source_field": "data.bang_topic.topic_list[].topic_url" if verified_shape else "listing_endpoint_fallback",
            "evidence": snapshot["evidence"], "rank_at_observation": position,
            "rank_source_field": "data.bang_topic.topic_list array position (1-based)",
            "provider_rank_raw": topic.get("idx_num"),
            "source_fields": "data.bang_topic.topic_list[]",
            "timestamp_semantics": "create_time and data.timestamp preserved raw; publication semantics unverified",
            "interaction_semantics": "discuss_num/content_num/tag preserved raw; no inferred comment/view/heat count",
            "comments_collected": False, "media_downloaded": False})


async def collect(context, *, transport=None, interval=1.0):
    params = context.params
    mode = params.get("mode", "hot")
    if mode not in CAPABILITIES["modes"]:
        raise ValueError("Tieba discovery modes: hot, feed")
    limits = budgets(params)
    maximum_bytes = int(params.get("max_snapshot_bytes", 1_048_576))
    if not 1024 <= maximum_bytes <= 4_194_304:
        raise ValueError("max_snapshot_bytes must be 1024..4194304")
    cp = await context.checkpoint(STREAM)
    reused = cp is not None
    if cp:
        snapshot, offset = cp["cursor"]["snapshot"], cp["cursor"]["offset"]
    else:
        http = await open_http(context, platform="tieba", account=params.get("account"),
            transport=transport, headers={"User-Agent": "ResearchCrawler/0.1 (local public research)",
                                         "Referer": "https://tieba.baidu.com/"})
        client = DiscoveryClient(http, context, "tieba", interval=interval)
        try:
            body, evidence = await client.get_json(ENDPOINT, {})
            evidence = dict(evidence, source_job_id=context.job_id)
            if evidence["bytes"] > maximum_bytes:
                raise schema_error(evidence, "snapshot_byte_limit")
            snapshot = _snapshot(body, evidence, maximum_bytes)
        except DiscoveryUnavailable as error:
            return await unavailable_summary(context, "tieba", mode, error)
        offset = 0
    topics = snapshot["topics"]
    end = min(len(topics), max(offset, limits["maximum"]))
    records = [_record(topic, index + 1, snapshot) for index, topic in enumerate(topics[offset:end], offset)]
    complete = end == len(topics)
    summary = {"status": "complete" if complete else "partial", "platform": "tieba", "mode": mode,
        "feed_kind": CAPABILITIES["feed_kind"], "feed_alias_of": "hot" if mode == "feed" else None,
        "scope": "one_observed_public_hot_topics_snapshot", "coverage_complete": complete,
        "coverage": [{"complete": complete, "count": end, "snapshot_count": len(topics),
                      "reason": "snapshot_end" if complete else "record_limit"}],
        "snapshot_reused": reused, "network_acquired_this_run": not reused,
        "evidence": snapshot["evidence"], "refresh_requires_new_job": True}
    await context.commit_page(STREAM, records,
        next_cursor={"snapshot": snapshot, "offset": end}, exhausted=complete,
        expected_revision=cp["revision"] if cp else 0, metadata={"summary": summary})
    return summary
