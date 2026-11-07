"""Independent, bounded public search; provider snippets are discovery, not source text.

Public HTML/RSS endpoints are best-effort and have no availability guarantee.
Challenges are reported, never solved or represented as an empty successful search.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from ..core.jobs import BudgetExceeded, JobContext
from ..core.records import Record
from ..core.sessions import budget_for, controlled, open_http
from .source_selection import relevance


PROVIDERS = {
    "brave_html": "https://search.brave.com/search",
    "bing_rss": "https://www.bing.com/search",
    "duckduckgo_html": "https://html.duckduckgo.com/html/",
}


class SearchProviderError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected a public HTTP(S) source URL without credentials")
    # Keep query parameters: they can identify distinct source documents.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def challenge_reason(document: str) -> str | None:
    lower = document.lower()
    # Structural signals avoid rejecting ordinary articles merely discussing CAPTCHA.
    if any(marker in lower for marker in ('id="challenge-form"', 'class="anomaly-modal',
            'id="captcha', 'id="b_captcha', 'cf-chl-', 'id="px-captcha')):
        return "challenge_required"
    if document.lstrip().startswith("<?xml") or document.lstrip().startswith("<rss"):
        return None
    soup = BeautifulSoup(document, "lxml")
    if soup.select_one("#challenge-form, .anomaly-modal, #captcha, #b_captcha, #px-captcha"):
        return "challenge_required"
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(marker in title for marker in ("just a moment", "access denied", "verify you are human", "robot check")):
        return "challenge_required"
    return None


def _plain(value: str) -> str:
    return BeautifulSoup(value, "lxml").get_text(" ", strip=True)


def _hit(title, url, snippet, provider):
    if not isinstance(url, str):
        return None
    try:
        url = canonical_url(url)
    except (TypeError, ValueError):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    return {"title": _plain(title)[:500], "url": url, "snippet": _plain(snippet or "")[:2000],
            "provider": provider, "source_date": None, "source_date_field": None}


def parse_results(provider: str, document: str) -> list[dict]:
    reason = challenge_reason(document)
    if reason:
        raise SearchProviderError(reason)
    results = []
    if provider == "bing_rss":
        if "<!DOCTYPE" in document.upper() or "<!ENTITY" in document.upper():
            raise SearchProviderError("invalid_feed")
        try:
            root = ET.fromstring(document)
        except ET.ParseError as exc:
            raise SearchProviderError("unexpected_response") from exc
        channel = root.find("channel") if root.tag == "rss" else None
        if channel is None or channel.find("title") is None:
            raise SearchProviderError("unexpected_response")
        items = channel.findall("item")
        for item in items:
            hit = _hit(item.findtext("title"), item.findtext("link"), item.findtext("description"), provider)
            if hit:
                results.append(hit)
        if items and not results:
            raise SearchProviderError("invalid_result_urls")
    elif provider == "brave_html":
        soup = BeautifulSoup(document, "lxml")
        # Observed public HTML, 2026-11-07. Avoid generated answers, inline
        # question panels, advertisements and cached snippets from other cards.
        for block in soup.select('.snippet[data-type="web"]'):
            anchor = block.select_one('.result-content > a[href]')
            title = anchor.select_one('.title') if anchor else None
            if title is None:
                continue
            snippet = block.select_one('.generic-snippet')
            hit = _hit(title.get_text(' ',strip=True), anchor['href'],
                       snippet.get_text(' ',strip=True) if snippet else '', provider)
            if hit:
                results.append(hit)
        # No real empty-result DOM was observed. Fail closed rather than infer
        # success from an HTTP 200 consent/login/challenge or changed template.
        if not results:
            raise SearchProviderError('unexpected_response')
    elif provider == "duckduckgo_html":
        soup = BeautifulSoup(document, "lxml")
        for block in soup.select(".result"):
            if "result--ad" in block.get("class", []):
                continue
            anchor = block.select_one("a.result__a[href]")
            if anchor is None:
                continue
            target = urljoin(PROVIDERS[provider], anchor["href"])
            parts = urlsplit(target)
            if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
                target = parse_qs(parts.query).get("uddg", [""])[0]
            snippet = block.select_one(".result__snippet")
            hit = _hit(anchor.get_text(" ", strip=True), target, snippet.get_text(" ", strip=True) if snippet else "", provider)
            if hit:
                results.append(hit)
        if not results and soup.select_one(".no-results, .no-results__message") is None:
            raise SearchProviderError("unexpected_response")
    else:
        raise ValueError("Unsupported search provider")
    unique = {}
    for item in results:
        unique.setdefault(item["url"], item)
    return list(unique.values())


def parameters(params: dict):
    query = params.get("query", params.get("question", ""))
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValueError("query must contain 1..300 characters")
    limit = int(params.get("limit", 5))
    if not 1 <= limit <= 20:
        raise ValueError("limit must be 1..20")
    providers = params.get("providers", list(PROVIDERS))
    if not isinstance(providers, list) or not providers or any(p not in PROVIDERS for p in providers):
        raise ValueError("providers must contain bing_rss, brave_html or duckduckgo_html")
    return query.strip(), limit, list(dict.fromkeys(providers))


def scoped_context(parent: JobContext, params: dict) -> JobContext:
    """Separate stage parameters while retaining one job, resource owner and budget."""
    child = JobContext(parent.job_id, params, parent.store, parent.paths, parent.resources)
    child._network_budget = budget_for(parent)
    if not hasattr(parent, "_http_clients"):
        parent._http_clients = {}
    child._http_clients = parent._http_clients
    return child


async def request_provider(context, provider: str, query: str) -> dict:
    args = {"q": query, **({"format": "rss"} if provider == "bing_rss" else
                          {"source":"web"} if provider == "brave_html" else {})}
    url = PROVIDERS[provider] + "?" + urlencode(args)
    request_url = url
    client = await open_http(context, platform="search", headers={"User-Agent": "ResearchCrawler/0.1 (local research)"},
                             timeout=min(30, max(1, float(context.params.get("timeout_seconds", 15)))))
    for _ in range(6):
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = canonical_url(urljoin(url, response.headers.get("location", "")))
                continue
            if response.status_code >= 400:
                raise SearchProviderError("http_" + str(response.status_code))
            content = bytearray()
            async for part in response.aiter_bytes():
                content.extend(part)
                if len(content) > 2_000_000:
                    raise SearchProviderError("response_byte_limit")
            document = bytes(content).decode(response.encoding or "utf-8", errors="replace")
            reason = challenge_reason(document)
            if reason:
                raise SearchProviderError(reason)
            if response.status_code != 200:
                raise SearchProviderError("unexpected_http_" + str(response.status_code))
            results = parse_results(provider, document)
            notice = None
            if provider == "bing_rss":
                notice = ET.fromstring(document).findtext("./channel/copyright")
            return {"results": results, "request_url": request_url, "response_url": str(response.url),
                    "response_sha256": hashlib.sha256(content).hexdigest(), "response_bytes": len(content),
                    "http_status": response.status_code, "provider_notice": notice}
    raise SearchProviderError("redirect_limit")


async def search_web(context: JobContext, *, provider_fetch=request_provider) -> dict:
    query, limit, providers = parameters(context.params)
    checkpoint = await context.checkpoint("discovery_search")
    if checkpoint and checkpoint["exhausted"]:
        return dict(checkpoint["metadata"]["summary"])
    revision = checkpoint["revision"] if checkpoint else 0
    previous = checkpoint["metadata"].get("summary", {}) if checkpoint else {}
    results = list(previous.get("results", []))
    candidates = list(previous.get("result_candidates", results))
    # Successful providers are retained; failed providers alone may be retried on resume.
    attempts = list(previous.get("providers", []))
    successful = {p["provider"] for p in attempts if p["status"] == "complete"}
    failed_before = {p["provider"] for p in attempts if p["status"] != "complete"}
    started = time.monotonic()
    max_seconds = min(120, max(1, float(context.params.get("search_max_seconds", 40))))
    for provider in providers:
        relevant_count = sum(relevance(item['title']+' '+item.get('snippet',''),query)['eligible'] for item in results)
        if provider in successful or (relevant_count >= limit and provider not in failed_before):
            continue
        await context.check_control()
        attempts = [p for p in attempts if p["provider"] != provider]
        records = []
        try:
            remaining = min(max_seconds - (time.monotonic() - started),
                            budget_for(context).maximum_seconds - (time.monotonic() - budget_for(context).started))
            if remaining <= 0:
                raise SearchProviderError("time_budget")
            async with asyncio.timeout(remaining):
                response = await controlled(context, provider_fetch(context, provider, query))
            existing = {item["url"] for item in candidates}
            # Keep a bounded provider pool before applying the user's output cap.
            # Otherwise an irrelevant first result permanently hides later hits.
            for rank, hit in enumerate(response["results"][:20], 1):
                url = canonical_url(hit["url"])
                if url in existing:
                    continue
                payload = {**hit, "url": url, "query": query, "provider_rank": rank,
                           "search_url": response["response_url"], "provider_response_sha256": response["response_sha256"],
                           "relevance":relevance(hit['title']+' '+hit.get('snippet',''),query)}
                record = Record("web_search", "result", provider + ":" + query + ":" + url, url, payload,
                                coverage={"complete": False, "scope": "search_result_snippet", "source_page_fetched": False})
                records.append(record)
                candidates.append({**payload, "reference": f"record:{record.key}@{record.content_hash}",
                                "record_key": record.key, "content_hash": record.content_hash,
                                "observed_at": record.observed_at, "coverage": record.coverage})
                existing.add(url)
            # Once a snippet meets the bounded relevance gate, keep the engine's
            # rank. Keyword-stuffed summaries must not outrank its first result.
            def rank_candidate(item):
                score = relevance(item['title']+' '+item.get('snippet',''),query)
                return (score['eligible'], 0 if score['eligible'] else len(score['matched_terms']))
            results = sorted(candidates, key=rank_candidate, reverse=True)[:limit]
            attempts.append({"provider": provider, "status": "complete", "returned": len(response["results"]),
                             **{k: v for k, v in response.items() if k != "results"}})
        except (SearchProviderError, httpx.HTTPError, TimeoutError, BudgetExceeded) as exc:
            attempts.append({"provider": provider, "status": "unavailable", "reason": getattr(exc, "code", getattr(exc, "reason", type(exc).__name__))})
        failed = [p for p in attempts if p["status"] != "complete"]
        summary = {"status": "partial" if failed else "complete", "mode": "public_web_search", "query": query,
                   "search_status": "results_found" if results else "no_results" if any(p["status"] == "complete" for p in attempts) else "providers_unavailable",
                   "results": results, "result_candidates": candidates, "returned": len(results), "limit": limit, "providers": attempts,
                   "selection_strategy":"bounded_snippet_lexical_v1", "relevant_returned":sum(
                       relevance(item['title']+' '+item.get('snippet',''),query)['eligible'] for item in results),
                   "provider_failures": failed, "network_search_used": True, "model_used": False,
                   "coverage": {"scope": "bounded_provider_results", "full_web_coverage": False,
                                "source_pages_fetched": False}, "elapsed_seconds": round(time.monotonic() - started, 3)}
        checkpoint = await context.commit_page("discovery_search", records, next_cursor=None,
            exhausted=False, expected_revision=revision, metadata={"summary": summary})
        revision = checkpoint["revision"]
    # Final commit also marks provider work complete when fewer results than requested exist.
    if not attempts:
        raise ValueError("No search provider was attempted")
    failed = [p for p in attempts if p["status"] != "complete"]
    summary = {**(checkpoint["metadata"]["summary"] if checkpoint else previous),
               "status": "partial" if failed else "complete", "providers": attempts, "provider_failures": failed,
               "requests": budget_for(context).requests, "network_bytes": budget_for(context).bytes}
    await context.commit_page("discovery_search", [], next_cursor=None, exhausted=not failed,
        expected_revision=revision, metadata={"summary": summary})
    return summary
