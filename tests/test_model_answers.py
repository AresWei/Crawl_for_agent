import json

import pytest

from research_crawler.adapters.analysis import answer_model
from research_crawler.core.jobs import JobRunner
from research_crawler.core.paths import AppPaths
from research_crawler.core.records import Record
from research_crawler.core.store import Store
from research_crawler.intelligence.local_model import ModelCompletion
from research_crawler.search import LocalSearch


def runtime(tmp_path, outputs):
    paths = AppPaths.discover(tmp_path)
    paths.initialize()
    store = Store(paths.database)
    seed = store.create_job("fixture", {})
    store.transition(seed["id"], "running")
    original = Record("web", "page", "one", "https://example.test", {"title": "Telescope report", "text": "A telescope observed a star in 2025."})
    store.commit_page(seed["id"], "page", [original], next_cursor=None, exhausted=True)
    store.transition(seed["id"], "complete")
    class Model:
        def __init__(self, root): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def complete(self, messages, **kwargs):
            output = outputs.pop(0)
            if 'queries' in output:
                assert kwargs['response_format']['type'] == 'json_schema'
                assert kwargs['response_format']['json_schema']['name'] == 'local_query_rewrite'
            return ModelCompletion(json.dumps(output), "test-model", {}, 1.0, "stop")
    runner = JobRunner(store, paths)
    async def handler(ctx): return await answer_model(ctx, model_factory=Model)
    runner.register("answer.model", handler)
    return store, runner, original


@pytest.mark.asyncio
async def test_answer_keeps_original_evidence_and_excludes_generated_text_from_index(tmp_path):
    store, runner, original = runtime(tmp_path, [{"claims": [{"text": "2025年观测到一颗恒星。", "citations": [1],
        "supports":[{"citation":1,"quote":"A telescope observed a star in 2025."}]}],
        "insufficient": False, "limits": "仅基于来源摘录。"},
        {"checks":[{"claim":0,"reason":"The source states this observation.","verdict":"supported",
            "assertion_checks":[{"assertion":0,"support_ids":[0],"reason":"The selected quote states this observation.","verdict":"supported"}],
            "context_conflict":False}]}])
    job = store.create_job("answer.model", {"query": "telescope", "question": "观测到了什么？"})
    result = await runner.run(job["id"])
    assert result["status"] == "complete"
    summary = result["summary"]
    assert summary["citation_ids_validated"] and not summary["semantic_accuracy_automatically_proven"]
    assert summary["evidence"][0]["content_hash"] == original.content_hash
    assert summary["evidence"][0]["excerpt"] == original.payload["text"]
    assert summary["answer"].endswith("[1]")
    assert LocalSearch(store).search("telescope")["index"]["indexed_records"] == 1


@pytest.mark.asyncio
async def test_nonexistent_model_citation_is_not_published(tmp_path):
    store, runner, _ = runtime(tmp_path, [{"claims": [{"text": "Unsupported", "citations": [9]}], "insufficient": False, "limits": ""}])
    job = store.create_job("answer.model", {"query": "telescope"})
    result = await runner.run(job["id"])
    assert result["status"] == "partial"
    assert result["summary"]["answer"] is None
    assert result["summary"]["answer_status"] == "invalid_or_truncated_model_output"
    assert not store.checkpoint(job["id"], "model_answer")["exhausted"]


@pytest.mark.asyncio
async def test_query_rewrite_without_evidence_reports_actual_model_use(tmp_path):
    store, runner, _ = runtime(tmp_path, [{"queries": []}])
    job = store.create_job("answer.model", {"query": "unavailable concept"})
    result = await runner.run(job["id"])
    assert result["summary"]["model_used"] is True
    assert result["summary"]["answer_status"] == "no_evidence"
    assert result["summary"]["answer"] is None


@pytest.mark.asyncio
async def test_unavailable_account_preserves_retrieved_original(tmp_path, monkeypatch):
    from research_crawler.adapters import analysis
    from research_crawler.intelligence.local_model import LocalModelUnavailable
    store, _, original = runtime(tmp_path, [])
    class Unavailable:
        async def __aenter__(self): raise LocalModelUnavailable('Login unavailable')
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(analysis, 'text_client', lambda *args: Unavailable())
    runner = JobRunner(store, AppPaths.discover(tmp_path))
    runner.register('answer.model', answer_model)
    job = store.create_job('answer.model', {'query':'telescope','answer_backend':'codex'})
    result = await runner.run(job['id'])
    assert result['status'] == 'partial'
    assert result['summary']['answer_status'] == 'codex_account_unavailable'
    assert result['summary']['evidence'][0]['content_hash'] == original.content_hash
    assert result['summary']['answer'] is None
    assert not store.checkpoint(job['id'], 'model_answer')['exhausted']


@pytest.mark.asyncio
async def test_semantic_retrieval_partial_propagates_to_answer(tmp_path,monkeypatch):
    from research_crawler.search import semantic
    store,runner,original=runtime(tmp_path,[{'claims':[{'text':'来源记录了恒星观测。','citations':[1],
        'supports':[{'citation':1,'quote':'A telescope observed a star in 2025.'}]}],
        'insufficient':False,'limits':'仅摘录。'},
        {'checks':[{'claim':0,'reason':'The source states this observation.','verdict':'supported',
            'assertion_checks':[{'assertion':0,'support_ids':[0],'reason':'The selected quote states this observation.','verdict':'supported'}],
            'context_conflict':False}]}])
    async def retrieve(context,params):
        found=LocalSearch(store).search('telescope',1)
        return {**found,'coverage_complete':False,'coverage_limits':['embedding_input_truncated'],
            'embedding':{'model':'fixture-embedding'},'query_coverage':{'truncated':False},'cache':{}}
    monkeypatch.setattr(semantic,'retrieve_semantic',retrieve)
    job=store.create_job('answer.model',{'query':'星空观测','retrieval':'semantic'})
    result=await runner.run(job['id'])
    assert result['status']=='partial'
    assert result['summary']['retrieval_method']=='semantic'
    assert result['summary']['citation_ids_validated'] is True
    assert result['summary']['retrieval_coverage']['coverage_complete'] is False
    assert not store.checkpoint(job['id'],'model_answer')['exhausted']


@pytest.mark.asyncio
async def test_local_answer_rejects_semantically_wrong_claim_with_exact_quote(tmp_path):
    store,runner,_=runtime(tmp_path,[{'claims':[{'text':'观测证明恒星有生命。','citations':[1],
        'supports':[{'citation':1,'quote':'A telescope observed a star in 2025.'}]}],
        'insufficient':False,'limits':''},
        {'checks':[{'claim':0,'reason':'Observation does not establish life.','verdict':'unsupported',
            'assertion_checks':[{'assertion':0,'support_ids':[],'reason':'Observation does not establish life.','verdict':'unsupported'}],
            'context_conflict':False}]}])
    result=await runner.run(store.create_job('answer.model',{'query':'telescope'})['id'])
    assert result['status']=='partial' and result['summary']['answer'] is None
    assert len(result['summary']['rejected_claims'])==1
    assert result['summary']['draft_output']
