"""Search -> fetched source evidence -> local model, in one resumable job."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time

import httpx

from ..intelligence.grounded_answer import GENERATION_CONTRACT, GROUNDED_SCHEMA, generate_grounded_answer
from ..core.jobs import BudgetExceeded, answer_backend_metadata, answer_unavailable_status
from ..core.store import validate_answer_params
from ..core.records import Record
from ..core.sessions import budget_for, controlled
from ..intelligence.local_model import LocalModelClient, LocalModelUnavailable
from ..search.remote import challenge_reason, scoped_context, search_web
from ..search.source_selection import select_excerpt, follow_candidates
from ..search.research_evidence import enrich_evidence
from ..search.planned_evidence import enrich_planned_evidence
from ..search.full_evidence import enrich_full_evidence
from ..intelligence.text_client import text_client
from ..intelligence.answer_review import CONTRACT as REVIEW_CONTRACT
from ..web.local_content_filter import _model_identity
from ..web.runtime import LimitReached, WebSession, extract_document

CAPABILITIES = {
    "status": "bounded_public_providers", "source": "independently authored public RSS/HTML discovery",
    "implemented": ["web_search", "source_page_fetch", "local_model_cited_research_answer", "resume", "provider_failure_reporting"],
    "providers": ["brave_html", "bing_rss", "duckduckgo_html"], "paid_fallback": False, "codex_tool_backend": False,
    "answer_backends": ["local", "codex", "api"], "ui_preferred_backend": "codex",
    "codex_account": {"network_required": True, "account_quota_used": True, "api_key_used": False},
    "jobs": {"web.search": {"query": "1..300 characters", "limit": "1..20, default 5"},
             "research.answer": {"query": "1..300 characters", "question": "optional 1..1500 characters",
                                 "fetch_top": "1..5, default 3", "limit": "1..20, default 5",
                                 "source_attempt_limit": "fetch_top..20, default min(20, fetch_top*3)",
                                 "answer_backend": "new jobs default codex; persisted legacy jobs without this field retain local",
                                 "evidence_selection": "lexical, hybrid, planned, or context (Codex/API)",
                                 "api_profile": "required named configured profile when answer_backend=api",
                                 "source_context_chars": "context mode: Codex default 96000/max120000; API default/max64000"}},
    "limits": ["public providers can block or change", "snippets are not full-page evidence", "citation IDs do not prove semantic accuracy"],
}


RESEARCH_SCHEMA = GROUNDED_SCHEMA
ANSWER_REUSE_VERSION = 'research-finished-answer-reuse-v1'


def answer_input_key(context, model, question, evidence):
    identity = _model_identity(context.paths.root, model)
    if not identity:
        return None  # Unknown injected/model identity cannot safely reuse a response.
    value = {'version':ANSWER_REUSE_VERSION, 'review_contract':REVIEW_CONTRACT,
             'generation_contract':GENERATION_CONTRACT,
             'answer_schema':GROUNDED_SCHEMA, 'question':question,
             'backend':context.params.get('answer_backend','local'), 'model_identity':identity,
             'api_profile':context.params.get('api_profile'),
             'evidence':evidence, 'selection':context.params.get('evidence_selection','lexical'),
             'source_context_chars':context.params.get('source_context_chars',96000),
             'max_tokens':context.params.get('max_tokens',640)}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _excerpt(payload, query):
    return select_excerpt(payload,query)[:3]


async def fetch_sources(context, found: dict, *, session_factory=WebSession) -> dict:
    maximum = int(context.params.get("fetch_top", 3))
    if not 1 <= maximum <= 5:
        raise ValueError("fetch_top must be 1..5")
    checkpoint = await context.checkpoint("discovery_sources")
    prior = checkpoint["metadata"].get("summary", {}) if checkpoint else {}
    evidence = list(prior.get("evidence", []))
    revision = checkpoint["revision"] if checkpoint else 0
    pages = list(prior.get('pages',[]))
    done = {item["requested_url"] for item in evidence} | {item['url'] for item in pages}
    errors = list(prior.get('errors', []))
    params = {**context.params, "render": "http", "max_requests": budget_for(context).maximum_requests,
              "max_html_bytes": min(2_000_000, int(context.params.get("max_html_bytes", 2_000_000))),
              "prune": True}
    session = session_factory(scoped_context(context, params))
    follow_limit = context.params.get('source_follow_links',3)
    if type(follow_limit) is not int or not 0<=follow_limit<=5:
        raise ValueError('source_follow_links must be an integer 0..5')
    attempt_limit = context.params.get('source_attempt_limit', min(20,maximum*3))
    if type(attempt_limit) is not int or not maximum <= attempt_limit <= 20:
        raise ValueError('source_attempt_limit must be an integer between fetch_top and 20')
    # fetch_top limits accepted evidence, not the search candidates we may inspect.
    selected = list(prior.get('queue',found['results'][:20]))
    queued = {hit['url'] for hit in selected}
    # A recovered search provider may add useful URLs after the old queue ended.
    # Keep acquired versions/citation order; add only bounded current search hits.
    for hit in found['results'][:20]:
        if hit['url'] not in queued:
            selected.append(hit)
            queued.add(hit['url'])
    if checkpoint and checkpoint['exhausted'] and (len(evidence)>=maximum or all(hit['url'] in done for hit in selected)):
        return dict(prior)
    # A full run of old failures must not consume every resumed attempt before
    # newly recovered provider URLs are tried. Keep failures for later retries.
    failed_urls = {error['url'] for error in errors}
    selected.sort(key=lambda hit: hit['url'] in failed_urls)
    followed = sum(bool(hit.get('parent_reference')) for hit in selected)
    summary = {"evidence": evidence, "errors": errors, "requested": len(selected),'pages':pages,'queue':selected,
               "fetched":len(pages),'accepted_for_answer':len(evidence),"source_scope":"response_html_excerpts"}
    attempted = 0
    for hit in selected:
        if len(evidence)>=maximum:
            break
        if hit["url"] in done:
            continue
        if attempted >= attempt_limit:
            break
        attempted += 1
        records = []
        try:
            remaining = budget_for(context).maximum_seconds - (time.monotonic() - budget_for(context).started)
            async with asyncio.timeout(max(.01, remaining)):
                document, final_url, status, artifacts, mode = await controlled(context, session.collect(hit["url"]))
            if challenge_reason(document):
                raise ValueError("challenge_required")
            if isinstance(status,int) and status>=400:
                raise ValueError('http_error_response')
            payload = await controlled(context, asyncio.to_thread(extract_document, document, final_url, params))
            payload.update({"requested_url": hit["url"], "final_url": final_url, "status_code": status,
                            "artifacts": artifacts, "render_mode": mode, "depth": 0})
            field, excerpt, span, selection = await controlled(context,asyncio.to_thread(select_excerpt,payload,found['query'],question=context.params.get('question')))
            record = Record("web", "page", hit["url"], final_url, payload,
                            coverage={"complete": True, "scope": "response_html", "dynamic_content_evaluated": False})
            records.append(record)
            reference = f'record:{record.key}@{record.content_hash}'
            pages.append({'url':hit['url'],'final_url':final_url,'reference':reference,'selection':selection,
                          'parent_reference':hit.get('parent_reference')})
            done.add(hit['url'])
            if followed < follow_limit and len(evidence)+int(selection['eligible']) < maximum:
                for target in follow_candidates(payload,found['query'],final_url,question=context.params.get('question')):
                    if target in queued:
                        continue
                    selected.append({'url':target,'reference':reference,'parent_reference':reference})
                    queued.add(target)
                    followed += 1
                    break  # At most one relevant same-host child per acquired page.
            if selection['eligible'] and len(evidence)<maximum:
                evidence.append({"citation": len(evidence) + 1, "url": final_url, "requested_url": hit["url"],
                "title": payload["title"], "reference": f"record:{record.key}@{record.content_hash}",
                "record_key": record.key, "content_hash": record.content_hash, "source_date": None,
                "source_date_field": None, "observed_at": record.observed_at, "discovery_reference": hit["reference"],
                "excerpt": excerpt, "excerpt_field": field, "excerpt_range": span,
                "coverage": {**record.coverage, "answer_uses": "exact_bounded_excerpt"},'selection':selection})
            errors = [error for error in errors if error['url'] != hit['url']]
        except (httpx.HTTPError, TimeoutError, ValueError, LimitReached, BudgetExceeded) as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {"challenge_required", "insufficient_page_text",'http_error_response'} else getattr(exc, "reason", type(exc).__name__)
            errors = [error for error in errors if error['url'] != hit['url']]
            errors.append({"url": hit["url"], "reason": reason})
        summary = {"evidence": evidence, "errors": errors, "requested": len(selected),'pages':pages,'queue':selected,
                   "fetched": len(pages), 'accepted_for_answer':len(evidence), "source_scope": "response_html_excerpts"}
        checkpoint = await context.commit_page("discovery_sources", records, next_cursor=None, exhausted=False,
            expected_revision=revision, metadata={"summary": summary})
        revision = checkpoint["revision"]
    summary.update(attempted_this_run=attempted, attempt_limit=attempt_limit,
                   pending=sum(hit['url'] not in done for hit in selected))
    await context.commit_page("discovery_sources", [], next_cursor=None,
        exhausted=not errors and (len(evidence)>=maximum or not summary['pending']),
        expected_revision=revision, metadata={"summary": summary})
    return summary


async def research_answer(context, *, search_handler=search_web, source_handler=fetch_sources, model_factory=None):
    validate_answer_params('research.answer', context.params)
    question = context.params.get("question", context.params.get("query", ""))
    if not isinstance(question, str) or not question.strip() or len(question) > 1500:
        raise ValueError("question must contain 1..1500 characters")
    if not 1 <= int(context.params.get("fetch_top", 3)) <= 5:
        raise ValueError("fetch_top must be 1..5")
    checkpoint = await context.checkpoint("discovery_answer")
    if checkpoint and checkpoint["exhausted"] and context.params.get('answer_backend') != 'api':
        return dict(checkpoint["metadata"]["summary"])
    found = await search_handler(context)
    sources = await source_handler(context, found) if found["results"] else {"evidence": [], "errors": [], "requested": 0, "fetched": 0}
    evidence = sources["evidence"]
    answer_key = None
    answer_result = None
    model = None
    summary = {"status": "partial", "mode": "web_research_local_model", "question": question,
               "query": found["query"], "search": found, "sources": sources, "evidence": evidence,
               "network_search_used": True, "model_used": False, "answer": None, "claims": [],
               "citation_ids_validated": False, "semantic_accuracy_automatically_proven": False}
    summary.update(answer_backend_metadata(context.params, summary))
    # The outer scheduling/control owner can cancel this coroutine at a budget
    # boundary. Share only the real progress; generate_once merges its audited
    # result in finally before controlled() finishes settling cancellation.
    context.answer_progress = summary
    if not evidence:
        summary.update(answer_status="no_fetched_evidence", insufficient=True,
                       limits="没有成功读取的来源正文；搜索摘要不足以替代来源回答。")
    else:
        try:
            model = model_factory(context.paths.root) if model_factory else text_client(
                context.paths.root, context.params.get('answer_backend', 'local'),
                **({'api_profile': context.params['api_profile']} if context.params.get('answer_backend') == 'api' else {}))
            context.answer_model_client = model
            model = await context.resources.enter_async_context(model)
            if context.params.get('evidence_selection')=='context':
                if context.params.get('answer_backend') not in {'codex', 'api'}:
                    raise ValueError('Full source context requires the Codex or API backend')
                selection_context = context
                if context.params.get('answer_backend') == 'api':
                    selection_context = scoped_context(context, {**context.params,
                        'source_context_chars': context.params.get('source_context_chars', 64000)})
                selected = await enrich_full_evidence(selection_context,sources)
            elif context.params.get('evidence_selection')=='planned':
                selected = await enrich_planned_evidence(context,model,sources,question,found['query'])
            else:
                selected = await enrich_evidence(context,sources,question=question,query=found['query'])
            evidence = selected['evidence']
            summary.update(evidence=evidence,evidence_selection=selected['selection'])
            summary['mode'] = {'codex': 'web_research_codex_account', 'api': 'web_research_api_model'}.get(
                context.params.get('answer_backend'), 'web_research_local_model')
            answer_key = answer_input_key(context,model,question,evidence)
            cached = checkpoint['metadata'] if checkpoint else {}
            if answer_key and cached.get('answer_input_key')==answer_key and cached.get('answer_completed'):
                answer_result = copy.deepcopy(cached['answer_result'])
                summary.update(answer_result,answer_reused=True,answer_model_calls_this_run=0)
            else:
                async def generate_once():
                    nonlocal answer_result
                    progress = {}
                    try:
                        result = await generate_grounded_answer(model,question,evidence,
                            max_tokens=context.params.get('max_tokens',640),progress=progress)
                        if (result.get('answer_status') in {'grounded_output','insufficient_evidence'}
                                and result.get('exact_support_quotes_validated')):
                            answer_result = copy.deepcopy(result)
                        return result
                    finally:
                        # Include the finished draft/review even if the enclosing
                        # control check raises immediately after model completion.
                        summary.update(progress)
                        summary.update(answer_backend_metadata(context.params, summary, model))
                summary.update(await controlled(context,generate_once()))
                summary['answer_reused']=False
            if found['status']=='partial' or sources['errors']:
                summary['status']='partial'
        except LocalModelUnavailable:
            summary.update(status='partial',answer_status=answer_unavailable_status(context.params),
                           insufficient=True, limits="所选回答后端当前不可用；已保留来源及已核对的部分回答。" if summary.get('claims') else "所选回答后端当前不可用；已保留搜索与原文证据。")
        except BudgetExceeded as exc:
            summary.update(status='partial',answer_status="budget_exceeded", insufficient=True, reason=exc.reason,
                           limits="达到任务预算；已保存来源及已核对的部分回答。" if summary.get('claims') else "达到任务预算；已保留搜索与原文证据，未发布模型结论。")
        except (ValueError, TypeError, httpx.HTTPError):
            summary.update(status='partial',answer_status="invalid_or_truncated_model_output", insufficient=True,
                           limits="后续模型响应或引用校验失败；已保存来源及此前核对的部分回答。" if summary.get('claims') else "模型响应或引用校验失败，未发布其结论。")
    summary.update(answer_backend_metadata(context.params, summary, model))
    summary.update(requests=budget_for(context).requests, network_bytes=budget_for(context).bytes,
                   elapsed_seconds=round(time.monotonic() - budget_for(context).started, 3))
    record = Record("local_model", "research_answer", context.job_id, "", summary,
                    coverage={"generated": True, "scope": "fetched_source_excerpts", "evidence_count": len(evidence)})
    await context.commit_page("discovery_answer", [record], next_cursor=None, exhausted=summary["status"] == "complete",
        expected_revision=checkpoint["revision"] if checkpoint else 0,
        metadata={"summary":dict(summary), 'answer_input_key':answer_key,
                  'answer_completed':bool(answer_key and answer_result is not None), 'answer_result':answer_result})
    return summary


def register(runner):
    runner.register("web.search", search_web)
    slot = asyncio.Semaphore(1)
    async def scheduled(context):
        async with slot:
            return await research_answer(context)
    async def handler(context):
        try:
            return await controlled(context, scheduled(context))
        except BudgetExceeded as error:
            saved = context.answer_budget_summary(error.reason)
            if saved is not None:
                error.summary = saved
            raise
    runner.register("research.answer", handler)
