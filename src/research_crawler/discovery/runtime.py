"""Durable, bounded driver around migrated discovery algorithms and shared core."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..core.jobs import BudgetExceeded
from ..core.records import Record
from ..core.sessions import budget_for, controlled, open_http
from ..search.remote import canonical_url, challenge_reason
from .parsers import parse_sitemap, parse_robots, parse_feed, parse_homepage, parse_cc, fingerprint
from .source_algorithms import SourceAlgorithms, Soft404Fingerprint, _match, _parse_head, HAS_BM25
from .archives import SERVICE_HOSTS, cc_endpoint, cc_pages, wayback_endpoint, parse_wayback, crt_endpoint, parse_crt
from .cache import DiscoveryCache

SOURCES = {"sitemap", "robots", "cc", "homepage", "feed", "probe", "wayback", "crt"}
CC_CATALOG = "https://index.commoncrawl.org/collinfo.json"
USER_AGENT = "ResearchCrawler/0.1 (local research)"


def _bounded(params, name, default, maximum):
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be 1..{maximum}")
    return value


def settings(params, mapping):
    target = params.get("url", params.get("domain", ""))
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Supply a website url or domain")
    target = canonical_url(target if "://" in target else "https://" + target)
    origin = urlsplit(target).scheme + "://" + urlsplit(target).netloc
    hosts = params.get("allowed_hosts", [])
    if not isinstance(hosts, list) or len(hosts) > 10:
        raise ValueError("allowed_hosts must list at most 10 explicit hosts")
    allowed = {urlsplit(target).hostname}
    origins = [origin]
    for host in hosts:
        if not isinstance(host, str) or urlsplit("https://" + host).netloc != host or "/" in host or "@" in host:
            raise ValueError("allowed_hosts requires hostnames without schemes or paths")
        allowed.add(urlsplit("https://" + host).hostname.lower())
        selected_origin = urlsplit(target).scheme + "://" + host.lower()
        # Preserve explicit ports; additional hosts do not inherit the target's
        # nonstandard port or path. No candidate host expands this list.
        urlsplit(selected_origin).port
        if selected_origin not in origins:
            origins.append(selected_origin)
    sources = params.get("sources", ["robots", "sitemap", "homepage"] if mapping else ["robots", "sitemap"])
    if not isinstance(sources, list) or not sources or any(source not in SOURCES for source in sources):
        raise ValueError("Invalid discovery sources")
    if not mapping and set(sources) - {"robots", "sitemap", "cc"}:
        raise ValueError("web.seed supports robots, sitemap and cc; use web.map for other sources")
    depth = params.get("max_depth", 2)
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 5:
        raise ValueError("max_depth must be 0..5")
    pattern = params.get("url_pattern", "*")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
        raise ValueError("url_pattern must contain 1..1000 characters")
    query = params.get("query", "")
    if not isinstance(query, str) or len(query) > 300:
        raise ValueError("query must contain at most 300 characters")
    threshold = params.get("score_threshold")
    if threshold is not None:
        if type(threshold) not in (int, float) or not math.isfinite(threshold):
            raise ValueError("score_threshold must be a finite number")
        if not query.strip():
            raise ValueError("score_threshold requires a query")
    if params.get("bm25") and not HAS_BM25:
        raise ValueError("BM25 dependency unavailable; no substitute score is claimed")
    paths = params.get("probe_paths", [])
    if "probe" in sources and (not isinstance(paths, list) or not 1 <= len(paths) <= 20 or
            any(not isinstance(path, str) or not path.startswith("/") or path.startswith("//") for path in paths)):
        raise ValueError("probe requires 1..20 explicit relative probe_paths")
    cc_index = params.get("cc_index")
    if cc_index and (not isinstance(cc_index, str) or not re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", cc_index)):
        raise ValueError("cc_index must identify a CC-MAIN-yyyy-ww index")
    return {"target": target, "origin": origin, "origins": origins, "hosts": allowed, "sources": list(dict.fromkeys(sources)),
            "max_urls": _bounded(params, "max_urls", 100, 1000),
            "max_documents": _bounded(params, "max_documents", 10, 50), "max_depth": depth,
            "max_document_bytes": _bounded(params, "max_document_bytes", 2_000_000, 10_000_000),
            "cc_max_pages": _bounded(params, "cc_max_pages", 3, 50),
            "wayback_max_pages": _bounded(params, "wayback_max_pages", 3, 50),
            "wayback_page_size": _bounded(params, "wayback_page_size", 100, 1000),
            "max_host_candidates": _bounded(params, "max_host_candidates", 100, 1000),
            "pattern": pattern, "query": query, "score_threshold": threshold, "cc_index": cc_index, "probe_paths": paths}


def in_scope(url, config):
    try:
        return urlsplit(canonical_url(url)).hostname in config["hosts"]
    except (TypeError, ValueError):
        return False


def work(kind, url, **extra):
    return {"kind": kind, "url": url, "depth": 0, **extra}


def _key(item):
    return item["kind"] + ":" + item["url"]


def cc_url(index, config, page=None, host=None):
    # Exact selected hostname; do not substitute upstream's implicit *.domain glob.
    return cc_endpoint(index, host or urlsplit(config["target"]).hostname, page=page)


def initial_state(config, params):
    queue = []
    sources = config["sources"]
    feed_urls = []
    if "feed" in sources:
        feed_urls = params.get("feed_urls", [])
        if not isinstance(feed_urls, list) or len(feed_urls) > 10:
            raise ValueError("feed_urls must contain at most 10 URLs")
        for url in feed_urls:
            if not in_scope(url, config):
                raise ValueError("feed_urls must be on selected allowed_hosts")
    for origin in config["origins"]:
        target = config["target"] if origin == config["origin"] else origin + "/"
        if ("robots" in sources or "sitemap" in sources or "probe" in sources or params.get("check_live") or params.get("extract_head")
                or (params.get("respect_robots", True) and set(sources) & {"homepage", "feed"})):
            queue.append(work("robots", origin + "/robots.txt", optional=True))
        if "sitemap" in sources:
            if urlsplit(target).path.lower().endswith((".xml", ".xml.gz")):
                queue.append(work("sitemap", target))
            else:
                queue.extend(work("sitemap", origin + path, optional=True) for path in ("/sitemap.xml", "/sitemap_index.xml"))
        if "homepage" in sources or "feed" in sources:
            queue.append(work("homepage", target))
        if "feed" in sources and not feed_urls:
            queue.append(work("feed", origin + "/feed", optional=True))
        if "probe" in sources:
            queue.append(work("soft404", origin + "/research-crawler-missing-" + hashlib.sha256(origin.encode()).hexdigest()[:12], optional=True))
            queue.extend(work("probe", origin + path, optional=True) for path in config["probe_paths"])
    queue.extend(work("feed", canonical_url(url)) for url in feed_urls)
    if "cc" in sources:
        if config["cc_index"]:
            queue.extend(work("cc_pages", cc_url(config["cc_index"], config, host=host), index=config["cc_index"], host=host) for host in sorted(config["hosts"]))
        else:
            queue.append(work("cc_catalog", CC_CATALOG))
    if "wayback" in sources:
        queue.extend(work("wayback", wayback_endpoint(host, config["wayback_page_size"]), host=host, page=0) for host in sorted(config["hosts"]))
    if "crt" in sources:
        host = urlsplit(config["target"]).hostname
        queue.append(work("crt", crt_endpoint(host), host=host))
    return {"pending": queue, "done": [], "failed": [], "results": {}, "documents": [],
            "excluded": 0, "limits": [], "robots": {}, "fingerprints": {}, "host_candidates": {}, "archive_pages": {}}


async def fetch_document(context, item, config):
    service_host = SERVICE_HOSTS.get(item["kind"])
    client = await open_http(context, platform="web_discovery" if service_host else "web", account=None if service_host else context.params.get("account"),
        headers={"User-Agent": USER_AGENT}, timeout=min(30, max(1, float(context.params.get("timeout_seconds", 15)))))
    url = item["url"]
    method = "HEAD" if item["kind"] == "validate" and not context.params.get("extract_head") else "GET"
    for _ in range(6):
        if not (urlsplit(url).hostname == service_host and urlsplit(url).scheme == 'https' if service_host else in_scope(url, config)):
            raise ValueError("redirect_outside_selected_hosts")
        async with client.stream(method, url) as response:
            if response.is_redirect:
                url = canonical_url(urljoin(url, response.headers.get("location", "")))
                continue
            content = bytearray()
            async for part in response.aiter_bytes():
                content.extend(part)
                if len(content) > config["max_document_bytes"]:
                    raise ValueError("document_byte_budget")
            return {"url": str(response.url), "status": response.status_code, "content": bytes(content),
                    "encoding": response.encoding or "utf-8", "method": method}
    raise ValueError("redirect_limit")


def _can_fetch(state, url):
    text = state["robots"].get(urlsplit(url).netloc)
    if text is None:
        return True
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser.can_fetch("ResearchCrawler", url)


def _enqueue(state, item, config):
    if item["kind"] not in SERVICE_HOSTS and not in_scope(item["url"], config):
        state["excluded"] += 1
        return
    key = _key(item)
    if key in state["done"] or any(_key(other) == key for other in state["pending"] + state["failed"]):
        return
    if item["depth"] > config["max_depth"]:
        if "depth_budget" not in state["limits"]:
            state["limits"].append("depth_budget")
        return
    if len(state["pending"]) >= 2000:
        if "frontier_budget" not in state["limits"]:
            state["limits"].append("frontier_budget")
        return
    state["pending"].append(item)


def _reference(record):
    return "record:" + record.key + "@" + record.content_hash


def _add_candidates(context, state, entries, item, document, config):
    changed = []
    algorithms = SourceAlgorithms()
    for entry in entries:
        try:
            url = canonical_url(entry["url"])
        except (TypeError, ValueError):
            continue
        if not in_scope(url, config) or not _match(url, config["pattern"]):
            state["excluded"] += 1
            continue
        if context.params.get("filter_assets", True) and algorithms._is_nonsense(url):
            state["excluded"] += 1
            continue
        if url not in state["results"] and len(state["results"]) >= config["max_urls"]:
            if "url_budget" not in state["limits"]:
                state["limits"].append("url_budget")
            continue
        prior = state["results"].get(url, {})
        evidence = {"source": item["kind"], "source_url": document.url, "reference": _reference(document),
                    "source_fetched_at": document.payload.get('source_fetched_at'),
                    "cache": document.payload.get('cache')}
        payload = {**prior, **entry, "url": url, "host": urlsplit(url).hostname,
                   "sources": sorted(set(prior.get("sources", [])) | {item["kind"]}),
                   "discovery_evidence": prior.get("discovery_evidence", []) + ([evidence] if evidence not in prior.get("discovery_evidence", []) else []),
                   "page_fetched": prior.get("page_fetched", False) or entry.get("page_fetched", False),
                   "live_checked": prior.get("live_checked", False) or entry.get("live_checked", False)}
        if config["query"]:
            payload["url_relevance_score"] = algorithms._calculate_url_relevance_score(config["query"], url)
        state["results"][url] = payload
        changed.append(url)
        if (context.params.get("check_live") or context.params.get("extract_head")) and item["kind"] != "probe":
            _enqueue(state, work("validate", url), config)
    return changed


async def discover(context, *, mapping=False, fetcher=fetch_document):
    config = settings(context.params, mapping)
    cache = DiscoveryCache(context)
    stream = "domain_mapping" if mapping else "url_seeding"
    checkpoint = await context.checkpoint(stream)
    if checkpoint and checkpoint["exhausted"]:
        return dict(checkpoint["metadata"]["summary"])
    state = checkpoint["cursor"] if checkpoint else initial_state(config, context.params)
    state.setdefault("host_candidates", {})
    state.setdefault("archive_pages", {})
    revision = checkpoint["revision"] if checkpoint else 0
    if checkpoint and state["failed"]:
        state["pending"] = state["failed"] + state["pending"]
        state["failed"] = []
    errors, attempts = [], 0
    budget = budget_for(context)
    # Defaults bound this operation even when called without frontend parameters.
    budget.maximum_requests = min(budget.maximum_requests, int(context.params.get("max_requests", 80)))
    budget.maximum_seconds = min(budget.maximum_seconds, float(context.params.get("max_seconds", 120)))
    budget.maximum_bytes = min(budget.maximum_bytes, int(context.params.get("max_network_bytes", 20_000_000)))
    if not checkpoint:
        checkpoint = await context.commit_page(stream, [], next_cursor=state, exhausted=False,
            expected_revision=0, metadata={"phase": "frontier_initialized"})
        revision = checkpoint["revision"]
    reason = None
    while state["pending"] and attempts < config["max_documents"]:
        await context.check_control()
        item = state["pending"][0]
        key = _key(item)
        if key in state["done"]:
            state["pending"].pop(0)
            continue
        records, changed = [], []
        try:
            if item["kind"] in {"probe", "validate", "homepage", "feed", "soft404"} and context.params.get("respect_robots", True) and not _can_fetch(state, item["url"]):
                state["documents"].append({"url": item["url"], "kind": item["kind"], "outcome": "robots_disallowed"})
                if "robots_disallowed" not in state["limits"]:
                    state["limits"].append("robots_disallowed")
                if item["kind"] == "validate" and item["url"] in state["results"]:
                    state["results"][item["url"]]["live_check_status"] = "robots_disallowed"
                    changed.append(item["url"])
            else:
                attempts += 1
                remaining = budget.maximum_seconds - (time.monotonic() - budget.started)
                if remaining <= 0:
                    raise BudgetExceeded("time_budget")
                async with asyncio.timeout(remaining):
                    response = await controlled(context, cache.acquire(item, config, fetcher))
                status, content, final_url = response["status"], response["content"], response["url"]
                text = content.decode(response.get("encoding", "utf-8"), errors="replace")
                if not 200 <= status < 300 and not (item.get("optional") and status in {404, 410}) and item["kind"] != "soft404":
                    raise ValueError("http_" + str(status))
                if challenge_reason(text):
                    raise ValueError("challenge_required")
                digest = hashlib.sha256(content).hexdigest()
                artifact = context.paths.run(context.job_id) / ("discovery-" + digest + ".bin")
                temporary = artifact.with_suffix(".tmp")
                temporary.write_bytes(content)
                temporary.replace(artifact)
                document = Record("web_discovery", "document", item["url"], final_url,
                    {"kind": item["kind"], "status_code": status, "method": response.get("method", "GET"),
                     "response_sha256": digest, "byte_length": len(content), "artifact": str(artifact),
                     "source_fetched_at": response['source_fetched_at'], "cache": response['cache']},
                    coverage={"scope": "discovery_document", "complete": True})
                records.append(document)
                state["documents"].append({"url": final_url, "kind": item["kind"], "status_code": status,
                    "outcome": "absent" if status in {404, 410} and item["kind"] != "soft404" else "observed", "reference": _reference(document),
                    "source_fetched_at": response['source_fetched_at'], "cache": response['cache']})
                entries = []
                if item["kind"] == "soft404":
                    state["fingerprints"][urlsplit(item["url"]).netloc] = asdict(fingerprint(status, content))
                elif status in {404, 410}:
                    pass
                elif item["kind"] == "robots":
                    if "<html" in text[:1000].lower():
                        raise ValueError("unexpected_robots_document")
                    parsed = parse_robots(text, final_url)
                    state["robots"][urlsplit(item["url"]).netloc] = text
                    if "sitemap" in config["sources"]:
                        for sitemap in parsed["sitemaps"]:
                            _enqueue(state, work("sitemap", sitemap), config)
                    if mapping and "robots" in config["sources"]:
                        entries = parsed["directives"]
                elif item["kind"] == "sitemap":
                    parsed = parse_sitemap(content, final_url, config["max_document_bytes"] * 2)
                    if parsed["kind"] == "sitemapindex":
                        for entry in parsed["entries"]:
                            _enqueue(state, work("sitemap", entry["url"], depth=item["depth"] + 1), config)
                    else:
                        entries = parsed["entries"]
                elif item["kind"] == "homepage":
                    entries, feeds = parse_homepage(text, final_url)
                    if "homepage" not in config["sources"]:
                        entries = []
                    if "feed" in config["sources"]:
                        for url in feeds:
                            _enqueue(state, work("feed", url), config)
                elif item["kind"] == "feed":
                    entries = parse_feed(content, final_url)
                elif item["kind"] == "cc_catalog":
                    indexes = json.loads(text)
                    if not isinstance(indexes, list) or not indexes or not isinstance(indexes[0], dict) or not isinstance(indexes[0].get('id'), str) or not re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", indexes[0]["id"]):
                        raise ValueError("invalid_common_crawl_catalog")
                    for host in sorted(config["hosts"]):
                        _enqueue(state, work("cc_pages", cc_url(indexes[0]["id"], config, host=host), index=indexes[0]["id"], host=host), config)
                elif item["kind"] == "cc_pages":
                    pages = cc_pages(text)
                    state["archive_pages"]["cc:" + item["host"]] = {"index": item["index"], "reported_pages": pages, "selected_pages": min(pages, config["cc_max_pages"])}
                    if pages > config["cc_max_pages"] and "cc_page_budget" not in state["limits"]:
                        state["limits"].append("cc_page_budget")
                    for page in range(min(pages, config["cc_max_pages"])):
                        _enqueue(state, work("cc", cc_url(item["index"], config, page=page, host=item["host"]), host=item["host"], index=item["index"], page=page), config)
                elif item["kind"] == "cc":
                    entries = parse_cc(text)
                    if len(entries) > config["max_urls"] and "cc_result_limit" not in state["limits"]:
                        state["limits"].append("cc_result_limit")
                elif item["kind"] == "wayback":
                    entries, resume_key = parse_wayback(text)
                    state["archive_pages"]["wayback:" + item["host"]] = {"last_page": item["page"], "has_more": bool(resume_key)}
                    if resume_key:
                        next_url = wayback_endpoint(item["host"], config["wayback_page_size"], resume_key)
                        if next_url == item["url"] or 'wayback:' + next_url in state['done']:
                            raise ValueError('repeated_wayback_resume_key')
                        if item["page"] + 1 >= config["wayback_max_pages"]:
                            if "wayback_page_budget" not in state["limits"]: state["limits"].append("wayback_page_budget")
                        else:
                            _enqueue(state, work("wayback", next_url, host=item["host"], page=item["page"] + 1), config)
                elif item["kind"] == "crt":
                    for candidate in parse_crt(text, item["host"]):
                        host = candidate['host']
                        if host not in state['host_candidates'] and len(state['host_candidates']) >= config['max_host_candidates']:
                            if 'host_candidate_budget' not in state['limits']: state['limits'].append('host_candidate_budget')
                            continue
                        payload = {**candidate, 'selected_for_collection': host in config['hosts'], 'live_checked': False,
                                   'discovery_evidence': {'source': 'crt', 'reference': _reference(document), 'source_url': final_url}}
                        state['host_candidates'][host] = payload
                        records.append(Record('web_discovery', 'host', config['target'] + ':' + host, 'https://' + host + '/', payload,
                            coverage={'scope': 'certificate_log_host_candidate', 'complete': False}))
                        if host in config['hosts']: entries.append({'url': 'https://' + host + '/', 'certificate_host_candidate': True})
                elif item["kind"] in {"probe", "validate"}:
                    fp = state["fingerprints"].get(urlsplit(item["url"]).netloc)
                    is_soft = SourceAlgorithms()._is_soft_404(status, content, Soft404Fingerprint(**fp) if fp else None)
                    if item["kind"] == "probe" and not is_soft:
                        entries = [{"url": final_url, "live_checked": True, "live_status": status,
                                    "page_fetched": True, "head_data": _parse_head(text)}]
                    elif item["kind"] == "validate":
                        result = state["results"][item["url"]]
                        result.update(live_checked=True, live_status=status, live_check_status="soft404" if is_soft else "http_ok",
                                      live_checked_this_job=response['cache']['network_acquired_this_job'],
                                      page_fetched=response.get("method") != "HEAD",
                                      validation_evidence={"reference": _reference(document), "url": final_url,
                                                           "method": response.get("method", "GET"), "observed_at": response['source_fetched_at'],
                                                           "cache": response['cache']})
                        if context.params.get("extract_head"):
                            result["head_data"] = _parse_head(text)
                        changed.append(item["url"])
                changed.extend(_add_candidates(context, state, entries, item, document, config))
                await cache.save(item, response, config)
            state["pending"].pop(0)
            state["done"].append(key)
        except (ValueError, httpx.HTTPError, TimeoutError, BudgetExceeded, OSError) as exc:
            # Preserve raw artifacts and successful sibling work. Never flatten
            # a failed external source into a successful empty discovery.
            reason_code = getattr(exc, "reason", None) or str(exc) if isinstance(exc, (ValueError, BudgetExceeded)) else type(exc).__name__
            if len(reason_code) > 100 or any(char in reason_code for char in ("/", "\\", "?")):
                reason_code = type(exc).__name__
            errors.append({"url": item["url"], "kind": item["kind"], "reason": reason_code})
            state["pending"].pop(0)
            state["failed"].append(item)
            if isinstance(exc, (BudgetExceeded, TimeoutError)):
                reason = getattr(exc, "reason", "time_budget")
        # Global BM25 depends on the currently discovered candidate corpus.
        # Publish selection once per bounded invocation, with explicit revisions
        # if resumed discovery changes a previously published URL's eligibility.
        for url in dict.fromkeys(changed) if config["score_threshold"] is None else []:
            payload = state["results"][url]
            records.append(Record("web_discovery", "url", config["target"] + ":" + url, url, payload,
                source_date=payload.get("lastmod"), source_date_field="sitemap.lastmod" if payload.get("lastmod") else None,
                coverage={"scope": "discovered_url", "source_page_fetched": payload.get("page_fetched", False),
                          "complete": False}))
        try:
            checkpoint = await context.commit_page(stream, records, next_cursor=state, exhausted=False,
                expected_revision=revision, metadata={"errors": errors, "attempts": attempts})
        except BudgetExceeded as error:
            # JobContext commits atomically *before* checking the latched budget.
            # Recover that committed revision without checking the same budget
            # again, then publish the bounded candidate selection below. No more
            # network work occurs. JobControl/coroutine cancellation still escape.
            checkpoint = await asyncio.to_thread(context.store.checkpoint, context.job_id, stream)
            revision = checkpoint["revision"]
            reason = error.reason
            break
        revision = checkpoint["revision"]
        if reason:
            break
    results = list(state["results"].values())
    if context.params.get("bm25") and config["query"] and results:
        algorithms = SourceAlgorithms()
        scores = algorithms._calculate_bm25_score(config["query"], [algorithms._extract_text_context(item.get("head_data", {})) or item["url"] for item in results])
        results = [dict(item, bm25_score=float(score)) for item, score in zip(results, scores)]
        results.sort(key=lambda item: item["bm25_score"], reverse=True)
    elif config["query"]:
        results.sort(key=lambda item: item.get("url_relevance_score", 0), reverse=True)
    selected_records = []
    score_filter = None
    if config["score_threshold"] is not None:
        settled = not state["pending"] and not state["failed"]
        field = "bm25_score" if context.params.get("bm25") else "url_relevance_score"
        selected = [item for item in results if item.get(field, 0) >= config["score_threshold"]]
        score_filter = {"status": "complete" if settled else "partial", "field": field,
                        "threshold": config["score_threshold"], "comparison": ">=",
                        "corpus_scope": "currently_discovered_candidates", "corpus_settled": settled,
                        "candidates": len(results), "excluded": len(results)-len(selected)}
        state["score_filter"] = score_filter
        published = set(state.get("score_published_urls", []))
        eligible = {item["url"] for item in selected}
        selected_results = []
        for payload in results:
            is_selected = payload["url"] in eligible
            if not is_selected and payload["url"] not in published:
                continue
            payload = {**payload, "source_usable": is_selected,
                       "score_selection": {**score_filter, "selected": is_selected}}
            selected_records.append(Record("web_discovery", "url", config["target"] + ":" + payload["url"], payload["url"], payload,
                source_date=payload.get("lastmod"), source_date_field="sitemap.lastmod" if payload.get("lastmod") else None,
                coverage={"scope": "discovered_url", "source_page_fetched": payload.get("page_fetched", False),
                          "eligible": is_selected, "complete": False}))
            if is_selected:
                selected_results.append(payload)
        state["score_published_urls"] = sorted(published | eligible)
        results = selected_results
    complete = not state["pending"] and not state["failed"] and not state["limits"] and reason is None
    summary = {"status": "complete" if complete else "partial", "mode": "domain_mapping" if mapping else "url_seeding",
        "target": config["target"], "allowed_hosts": sorted(config["hosts"]), "sources": config["sources"],
        "results": results, "returned": len(results), "documents": state["documents"], "errors": errors,
        "score_filter": score_filter,
        "discovery_cache": cache.evidence(),
        "pending": len(state["pending"]), "failed": len(state["failed"]), "excluded_outside_scope_or_filter": state["excluded"],
        "host_candidates": list(state["host_candidates"].values()), "archive_pages": state["archive_pages"],
        "reason": reason or ("document_budget" if state["pending"] else "source_errors" if state["failed"] else state["limits"][0] if state["limits"] else None),
        "coverage": {"scope": "selected_hosts_and_sources", "complete_within_selected_limits": complete,
                     "full_domain_coverage": False, "limits": state["limits"], "common_crawl_scope": "one_index_selected_host_pages" if "cc" in config["sources"] else None,
                     "ct_hosts_auto_expanded": False, "archive_records_are_live_pages": False},
        "requests": budget.requests, "network_bytes": budget.bytes, "elapsed_seconds": round(time.monotonic() - budget.started, 3)}
    await context.commit_page(stream, selected_records, next_cursor=state, exhausted=complete, expected_revision=revision,
                              metadata={"summary": summary})
    return summary
