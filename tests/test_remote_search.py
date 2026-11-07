import asyncio
import json
from functools import partial

import pytest
import httpx

from research_crawler.adapters.discovery import fetch_sources, research_answer
from research_crawler.core.jobs import JobRunner
from research_crawler.core.paths import AppPaths
from research_crawler.core.store import Store
from research_crawler.intelligence.local_model import ModelCompletion, LocalModelUnavailable
from research_crawler.search.remote import SearchProviderError, parse_results, search_web


RSS = '''<?xml version="1.0"?><rss version="2.0"><channel><title>Search telescope</title>
<item><title>Original report</title><link>https://source.test/report#section</link>
<description>Unverified search summary says a planet was seen.</description><pubDate>Thu, 06 Nov 2025 10:00:00 GMT</pubDate></item>
<item><title>Duplicate</title><link>https://source.test/report</link><description>same URL</description></item>
<item><title>Unsafe</title><link>javascript:alert(1)</link></item>
</channel></rss>'''
HTML = '''<html><body><div class="result"><a class="result__a"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsource.test%2Freport%23part">Report</a>
<a class="result__snippet">A &amp; B.</a></div></body></html>'''
BRAVE_HTML = '''<div class="snippet" data-type="web"><div class="result-content">
<a href="https://source.test/report"><div class="title">Report</div></a>
<div class="generic-snippet">A &amp; B.</div></div></div>'''


def runtime(tmp_path, handler, kind="web.search"):
    paths = AppPaths.discover(tmp_path)
    paths.initialize()
    store = Store(paths.database)
    runner = JobRunner(store, paths)
    runner.register(kind, handler)
    return store, runner


async def provider(context, name, query):
    return {"results": parse_results(name, RSS if name == "bing_rss" else BRAVE_HTML if name == 'brave_html' else HTML),
            "request_url": "https://search.test/?q=telescope", "response_url": "https://search.test/?q=telescope",
            "response_sha256": "fixture-response-hash", "response_bytes": len(RSS), "http_status": 200}


def test_parsers_preserve_real_urls_deduplicate_and_do_not_claim_source_dates():
    rss = parse_results("bing_rss", RSS)
    html = parse_results("duckduckgo_html", HTML)
    assert len(rss) == len(html) == 1
    assert rss[0]["url"] == html[0]["url"] == "https://source.test/report"
    assert rss[0]["source_date"] is None  # Search-feed dates are not source publication dates.
    assert html[0]["snippet"] == "A & B."


@pytest.mark.parametrize("provider,document,reason", [
    ("bing_rss", '<html><title>Just a moment...</title></html>', "challenge_required"),
    ("duckduckgo_html", '<form id="challenge-form">Verify</form>', "challenge_required"),
    ("duckduckgo_html", '<html><h1>Search</h1></html>', "unexpected_response"),
    ("bing_rss", '<html><h1>Search</h1></html>', "unexpected_response"),
    ("bing_rss", '<rss><channel><title>Search</title><item><title>Bad</title><link>javascript:x</link></item></channel></rss>', "invalid_result_urls"),
])
def test_challenges_and_unrecognized_empty_responses_are_failures(provider, document, reason):
    with pytest.raises(SearchProviderError, match=reason):
        parse_results(provider, document)


@pytest.mark.asyncio
async def test_provider_failure_retained_with_dedup_and_resumable_results(tmp_path):
    calls = []
    async def failing(ctx, name, query):
        calls.append(name)
        if name == "duckduckgo_html":
            raise SearchProviderError("challenge_required")
        return await provider(ctx, name, query)
    store, runner = runtime(tmp_path, partial(search_web, provider_fetch=failing))
    job = store.create_job("web.search", {"query": "telescope", "limit": 3})
    first = await runner.run(job["id"])
    assert first["status"] == "partial"
    result = first["summary"]
    assert result["returned"] == 1 and result["search_status"] == "results_found"
    assert result["provider_failures"][0]["reason"] == "challenge_required"
    assert result["results"][0]["coverage"]["scope"] == "search_result_snippet"
    assert not result["results"][0]["coverage"]["source_page_fetched"]
    store.transition(job["id"], "pending")
    second = await runner.run(job["id"])
    assert second["status"] == "partial"
    assert calls == ["brave_html", "bing_rss", "duckduckgo_html", "duckduckgo_html"]
    assert second["summary"]["results"] == result["results"]


@pytest.mark.asyncio
async def test_all_blocked_never_becomes_successful_empty_search(tmp_path):
    async def blocked(*args):
        raise SearchProviderError("challenge_required")
    store, runner = runtime(tmp_path, partial(search_web, provider_fetch=blocked))
    job = store.create_job("web.search", {"query": "telescope"})
    result = await runner.run(job["id"])
    assert result["status"] == "partial"
    assert result["summary"]["search_status"] == "providers_unavailable"
    assert len(result["summary"]["provider_failures"]) == 3


class SourceSession:
    def __init__(self, context):
        self.context = context
    async def collect(self, url):
        return ('<html><title>Telescope report</title><body><main><p>The telescope observed a distant star in 2025. '
                'This report describes the observation and its limits.</p></main></body></html>', url, 200, {}, "http")


class Model:
    calls = 0
    def __init__(self, root): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def complete(self, messages, **kwargs):
        type(self).calls += 1
        if kwargs.get('response_format',{}).get('json_schema',{}).get('name')=='claim_support_review':
            return ModelCompletion(json.dumps({'checks':[{'claim':0,'verdict':'supported','reason':'The excerpt states the observation.',
                'assertion_checks':[{'assertion':0,'support_ids':[0],'reason':'The selected quote states the observation.','verdict':'supported'}],
                'context_conflict':False}]}),
                'fixture-model',{},1.0,'stop')
        assert "distant star" in messages[-1]["content"]
        assert "planet was seen" not in messages[-1]["content"]
        return ModelCompletion(json.dumps({"claims": [{"text": "来源记录在2025年观测到一颗恒星。", "citations": [1],
            'supports':[{'citation':1,'quote':'The telescope observed a distant star in 2025.'}]}],
            "insufficient": False, "limits": "只基于网页摘录。"}), "fixture-model", {}, 1.0, "stop")


@pytest.mark.asyncio
async def test_research_answer_uses_fetched_page_and_immutable_source_reference(tmp_path):
    Model.calls = 0
    handler = partial(research_answer, search_handler=partial(search_web, provider_fetch=provider),
                      source_handler=partial(fetch_sources, session_factory=SourceSession), model_factory=Model)
    store, runner = runtime(tmp_path, handler, "research.answer")
    job = store.create_job("research.answer", {"query": "telescope", "question": "观测到了什么？", "limit": 1, "fetch_top": 1})
    result = await runner.run(job["id"])
    assert result["status"] == "complete", result
    summary = result["summary"]
    evidence = summary["evidence"][0]
    assert summary["citation_ids_validated"] and Model.calls == 2
    assert summary["answer"].endswith("[1]")
    assert not summary["semantic_accuracy_automatically_proven"]
    assert evidence["coverage"]["answer_uses"] == "exact_bounded_excerpt"
    with store.connection() as db:
        row = db.execute("SELECT payload_json FROM record_versions WHERE record_key=? AND content_hash=?",
                         (evidence["record_key"], evidence["content_hash"])).fetchone()
        payload = json.loads(row[0])
    span = evidence["excerpt_range"]
    assert evidence["excerpt"] == payload[evidence["excerpt_field"]][span["start"]:span["end"]]
    assert evidence["reference"] != evidence["discovery_reference"]
    assert store.job(job["id"])["params"] == {"query": "telescope", "question": "观测到了什么？", "limit": 1, "fetch_top": 1}


@pytest.mark.asyncio
async def test_challenge_source_is_not_sent_to_model(tmp_path):
    class Challenge(SourceSession):
        async def collect(self, url):
            return '<title>Just a moment...</title><p>Verify you are human</p>', url, 200, {}, "http"
    Model.calls = 0
    handler = partial(research_answer, search_handler=partial(search_web, provider_fetch=provider),
                      source_handler=partial(fetch_sources, session_factory=Challenge), model_factory=Model)
    store, runner = runtime(tmp_path, handler, "research.answer")
    job = store.create_job("research.answer", {"query": "telescope", "limit": 1})
    result = await runner.run(job["id"])
    assert result["status"] == "partial"
    assert result["summary"]["answer_status"] == "no_fetched_evidence"
    assert result["summary"]["sources"]["errors"][0]["reason"] == "challenge_required"
    assert Model.calls == 0


@pytest.mark.asyncio
async def test_resume_after_model_unavailable_reuses_sources_and_rejects_invalid_citation(tmp_path):
    calls = []
    class Source(SourceSession):
        async def collect(self, url):
            calls.append(url)
            return await super().collect(url)
    class BrokenModel(Model):
        async def complete(self, *args, **kwargs):
            raise LocalModelUnavailable("fixture unavailable")
    handler = partial(research_answer, search_handler=partial(search_web, provider_fetch=provider),
                      source_handler=partial(fetch_sources, session_factory=Source), model_factory=BrokenModel)
    store, runner = runtime(tmp_path, handler, "research.answer")
    job = store.create_job("research.answer", {"query": "telescope", "limit": 1})
    first = await runner.run(job["id"])
    assert first["status"] == "partial" and first["summary"]["answer_status"] == "local_model_unavailable"
    assert len(first["summary"]["evidence"]) == 1
    class InvalidModel(Model):
        async def complete(self, *args, **kwargs):
            return ModelCompletion(json.dumps({"claims": [{"text": "Unsupported", "citations": [99]}],
                "insufficient": False, "limits": ""}), "fixture-model", {}, 1., "stop")
    runner.handlers["research.answer"] = partial(research_answer,
        search_handler=partial(search_web, provider_fetch=provider),
        source_handler=partial(fetch_sources, session_factory=Source), model_factory=InvalidModel)
    store.transition(job["id"], "pending")
    second = await runner.run(job["id"])
    assert calls == ["https://source.test/report"]
    assert second["status"] == "partial" and second["summary"]["answer"] is None
    assert second["summary"]["answer_status"] == "invalid_or_truncated_model_output"


@pytest.mark.asyncio
async def test_cancelling_inflight_provider_closes_it_without_success(tmp_path):
    started, closed = asyncio.Event(), asyncio.Event()
    async def slow(*args):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            closed.set()
    store, runner = runtime(tmp_path, partial(search_web, provider_fetch=slow))
    job = store.create_job("web.search", {"query": "telescope"})
    running = asyncio.create_task(runner.run(job["id"]))
    await asyncio.wait_for(started.wait(), 2)
    store.request_action(job["id"], "cancel")
    result = await asyncio.wait_for(running, 2)
    assert result["status"] == "cancelled" and closed.is_set()
    assert store.checkpoint(job["id"], "discovery_search") is None


@pytest.mark.asyncio
async def test_real_provider_transport_preserves_notice_and_honors_shared_budget(tmp_path, monkeypatch):
    from research_crawler.search import remote
    from research_crawler.core.sessions import open_http as shared_http
    responses = []
    def respond(request):
        responses.append(str(request.url))
        if request.url.host == "www.bing.com":
            return httpx.Response(302, headers={"location": "https://cn.bing.com/search?format=rss&q=telescope"})
        return httpx.Response(200, text=RSS.replace("<title>Search telescope</title>",
            "<title>Search telescope</title><copyright>Fixture provider restrictions</copyright>"))
    transport = httpx.MockTransport(respond)
    async def fixture_http(context, **kwargs):
        return await shared_http(context, transport=transport, **kwargs)
    monkeypatch.setattr(remote, "open_http", fixture_http)
    store, runner = runtime(tmp_path, search_web)
    job = store.create_job("web.search", {"query": "telescope", "providers": ["bing_rss"], "max_requests": 1})
    result = await runner.run(job["id"])
    assert result["status"] == "partial"
    assert result["summary"]["provider_failures"][0]["reason"] == "request_budget"
    assert len(responses) == 1
    second = store.create_job("web.search", {"query": "telescope", "providers": ["bing_rss"], "max_requests": 3})
    result = await runner.run(second["id"])
    assert result["status"] == "complete"
    assert result["summary"]["providers"][0]["provider_notice"] == "Fixture provider restrictions"
    assert result["summary"]["requests"] == 2
