"""Local-model answers grounded in stored, versioned evidence."""
from __future__ import annotations

import asyncio
import json

from ..core.records import Record
from ..core.sessions import controlled
from ..intelligence.local_model import LocalModelClient, LocalModelUnavailable
from ..intelligence.grounded_answer import ANSWER_SCHEMA, validate_answer, generate_grounded_answer
from ..core.jobs import BudgetExceeded
import httpx
from ..search import LocalSearch
from ..intelligence.text_client import text_client

CAPABILITIES = {"implemented": ["local_model_cited_answer", "local_query_rewrite", "codex_account_cited_answer"],
                "providers": ["local-llama.cpp", "codex_chatgpt"], "default_backend": "codex",
                "legacy_missing_backend": "local",
                "answer_backend": {"local": "experimental local model", "codex": "existing ChatGPT login; network and account quota required"},
                "paid_fallback": False, "api_key_used": False}

async def answer_model(context, *, model_factory=None):
    params = context.params
    question = params.get("question", params.get("query", ""))
    query = params.get("query", question)
    if not isinstance(question, str) or not question.strip() or len(question) > 1500:
        raise ValueError("Question must contain 1..1500 characters")
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValueError("Supply a query of 1..300 characters")
    limit = int(params.get("limit", 5))
    if not 1 <= limit <= 10:
        raise ValueError("Evidence limit must be 1..10")
    checkpoint = await context.checkpoint("model_answer")
    if checkpoint and checkpoint["exhausted"]:
        return checkpoint["metadata"]["summary"]
    retrieval=params.get('retrieval','lexical')
    if retrieval not in {'lexical','semantic'}:raise ValueError('Unknown retrieval method')
    search = LocalSearch(context.store)
    if retrieval=='semantic':
        from ..search.semantic import retrieve_semantic
        found=await retrieve_semantic(context,{**params,'query':query,'limit':limit})
    else:
        found = await asyncio.to_thread(search.search, query, limit)
    evidence, queries, model_used = found["results"], [query], False
    try:
        model = await context.resources.enter_async_context(model_factory(context.paths.root) if model_factory else
            text_client(context.paths.root,params.get('answer_backend','local')))
    except LocalModelUnavailable:
        summary={'status':'partial','answer_status':'codex_account_unavailable' if params.get('answer_backend')=='codex' else 'local_model_unavailable',
            'answer':None,'claims':[],'evidence':evidence,'insufficient':True,'question':question,'queries':queries,
            'model_used':False,'network_search_used':False,'answer_backend':params.get('answer_backend','local'),
            'retrieval_method':retrieval,'limits':'所选回答后端当前不可用；已保留检索到的原文。'}
        record=Record('local_model','answer',context.job_id,'',summary,
            coverage={'generated':False,'scope':'retrieved_excerpts','evidence_count':len(evidence)})
        await context.commit_page('model_answer',[record],next_cursor=None,exhausted=False,
            expected_revision=checkpoint['revision'] if checkpoint else 0,metadata={'summary':summary})
        return summary
    if not evidence and retrieval=='lexical' and params.get("rewrite_query", True):
        planned = await controlled(context, model.complete([
            {"role": "system", "content": '为本地资料检索提取1至3个短关键词或专名。不回答问题。只输出JSON {"queries":["关键词"]}。'},
            {"role": "user", "content": question}], max_tokens=128, temperature=0,
            response_format={'type':'json_schema','json_schema':{'name':'local_query_rewrite','strict':True,
                'schema':{'type':'object','properties':{'queries':{'type':'array','maxItems':3,'items':{'type':'string'}}},
                          'required':['queries'],'additionalProperties':False}}}))
        model_used = True
        parsed = json.loads(planned.text)
        alternatives = parsed.get("queries", [])
        if not isinstance(alternatives, list):
            raise ValueError("Invalid local search query plan")
        seen = set()
        for alternative in alternatives[:3]:
            if not isinstance(alternative, str) or not alternative.strip() or len(alternative) > 100:
                continue
            queries.append(alternative)
            matches = await asyncio.to_thread(search.search, alternative, limit)
            for item in matches["results"]:
                if item["reference"] not in seen:
                    evidence.append(item)
                    seen.add(item["reference"])
        evidence = evidence[:limit]
    if not evidence:
        summary = {"status": "partial", "answer_status": "no_evidence", "answer": None, "evidence": [],
                   "question": question, "queries": queries, "model_used": model_used,
                   "message": "已采集资料中没有匹配证据，不能据此回答。", "network_search_used": False}
    else:
        for index, item in enumerate(evidence, 1):
            item["citation"] = index
        summary = {'status':'partial','mode':'local_model_evidence_answer',
                   'question':question,'queries':queries,'model_used':False,'network_search_used':False,
                   'evidence':evidence,'answer':None,'claims':[],'insufficient':True,
                   'citation_ids_validated':False,'semantic_accuracy_automatically_proven':False}
        context.answer_progress = summary
        try:
            summary.update(await controlled(context,generate_grounded_answer(
                model,question,evidence,max_tokens=params.get('max_tokens',640),progress=summary)))
        except LocalModelUnavailable:
            summary.update(status='partial',insufficient=True,
                           answer_status='codex_account_unavailable' if params.get('answer_backend')=='codex' else 'local_model_unavailable',
                           limits='所选回答后端暂不可用；已保留来源。')
        except BudgetExceeded as exc:
            summary.update(status='partial',insufficient=True,answer_status='budget_exceeded',reason=exc.reason,
                limits='达到任务预算；已保留来源和已核对的部分回答。' if summary.get('claims') else '达到任务预算；已保留来源，未发布结论。')
        except (ValueError,TypeError,httpx.HTTPError):
            summary.update(status='partial',insufficient=True,answer_status='invalid_or_truncated_model_output',
                limits='后续模型响应未通过；已保留来源和已核对的部分回答。' if summary.get('claims') else '模型响应未通过；未发布结论。')
    summary['answer_backend']=params.get('answer_backend','local')
    summary['account_quota_used']=summary['answer_backend']=='codex'
    summary['retrieval_method']=retrieval
    summary['embedding_model_used']=retrieval=='semantic'
    if retrieval=='semantic':
        summary['retrieval_coverage']={key:found[key] for key in ('coverage_complete','coverage_limits','index','embedding','query_coverage','cache')}
        if not found['coverage_complete']:
            summary['status']='partial'
    record = Record("local_model", "answer", context.job_id, "", summary,
                    coverage={"generated": True, "scope": "retrieved_excerpts", "evidence_count": len(evidence)})
    await context.commit_page("model_answer", [record], next_cursor=None, exhausted=summary["status"] == "complete",
        expected_revision=checkpoint["revision"] if checkpoint else 0, metadata={"summary": dict(summary)})
    return summary


def register(runner):
    slot = asyncio.Semaphore(1)
    async def scheduled(context):
        async with slot:
            return await answer_model(context)
    async def handler(context):
        try:
            return await controlled(context, scheduled(context))
        except BudgetExceeded as error:
            saved = context.answer_budget_summary(error.reason)
            if saved is not None:
                error.summary = saved
            raise
    runner.register("answer.model", handler)
