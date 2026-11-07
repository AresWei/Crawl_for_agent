"""Opt-in API task wiring. Synthetic models and sources; no provider requests."""
from contextlib import AsyncExitStack
from functools import partial
from types import SimpleNamespace

import pytest

from research_crawler.adapters import analysis, discovery
from research_crawler.core.jobs import BudgetExceeded, JobContext
from research_crawler.core.records import Record
from research_crawler.core.paths import AppPaths
from research_crawler.core.store import Store
from research_crawler.intelligence.local_model import LocalModelUnavailable
from test_model_answers import runtime as stored_runtime
from test_remote_search import Model, SourceSession, runtime


@pytest.mark.parametrize('extra', [
    {'answer_backend': 'other'}, {'answer_backend': {}},
    {'answer_backend': 'api'}, {'answer_backend': 'api', 'api_profile': '../private'},
    {'answer_backend': 'codex', 'api_profile': 'fixture'},
    {'api_key': 'fixture-do-not-store'},
    {'options': [{'access_token': 'fixture-do-not-store'}]},
    {'options': {'Authorization': 'fixture-do-not-store'}},
    {'answer_backend': 'api', 'api_profile': 'fixture', 'base_url': 'https://invalid.test'},
    {'answer_backend': 'api', 'api_profile': 'fixture', 'source_context_chars': 64001},
])
def test_invalid_or_secret_job_params_rejected_before_persistence(tmp_path, extra):
    store = Store(tmp_path / 'state' / 'research.db')
    with pytest.raises(ValueError):
        store.create_job('research.answer', {'query': 'telescope', **extra})
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 0


def test_defaults_api_profile_scope_and_context_budget(tmp_path):
    store = Store(tmp_path / 'state' / 'research.db')
    ordinary = store.create_job('research.answer', {'query': 'topic'})['params']
    assert ordinary['answer_backend'] == 'codex' and ordinary['evidence_selection'] == 'context'
    assert 'api_profile' not in ordinary and 'source_context_chars' not in ordinary
    api = store.create_job('research.answer', {'query': 'topic', 'answer_backend': 'api', 'api_profile': 'fixture'})['params']
    assert api['source_context_chars'] == 64000 and api['evidence_selection'] == 'context'
    small = store.create_job('research.answer', {**api, 'source_context_chars': 10000})['params']
    assert small['source_context_chars'] == 10000
    with pytest.raises(ValueError, match='only by'):
        store.create_job('comments.analyze', {'answer_backend': 'api', 'api_profile': 'fixture'})


class ApiModel(Model):
    revision = 'v1'
    def __init__(self, root):
        self.requests_attempted = self.requests_completed = 0
        self.cache_identity = {'model_id': 'fixture-model', 'provider': 'fixture-api',
                               'name': 'fixture', 'revision': type(self).revision}

    async def complete(self, *args, **kwargs):
        self.requests_attempted += 1
        result = await super().complete(*args, **kwargs)
        self.requests_completed += 1
        return result


async def found(ctx):
    return {'status': 'partial', 'query': 'telescope', 'results': [
        {'url': 'https://source.test/report', 'reference': 'fixture-hit'}]}


@pytest.mark.asyncio
async def test_research_api_context_cache_and_changed_model_identity(tmp_path, monkeypatch):
    instances = []
    def factory(root, backend, *, api_profile):
        assert backend == 'api' and api_profile == 'fixture'
        instance = ApiModel(root)
        instances.append(instance)
        return instance
    monkeypatch.setattr(discovery, 'text_client', factory)
    ApiModel.revision = 'v1'
    handler = partial(discovery.research_answer, search_handler=found,
                      source_handler=partial(discovery.fetch_sources, session_factory=SourceSession))
    store, runner = runtime(tmp_path, handler, 'research.answer')
    job = store.create_job('research.answer', {'query': 'telescope', 'fetch_top': 1,
        'answer_backend': 'api', 'api_profile': 'fixture'})
    first = await runner.run(job['id'])
    summary = first['summary']
    assert first['status'] == 'partial' and summary['answer_status'] == 'grounded_output', first
    assert summary['evidence_selection']['source_context_chars'] == 64000
    assert summary['api_selected'] and summary['api_key_used'] and summary['model_used_this_run']
    assert summary['api_requests_attempted'] == summary['api_requests_completed'] == 2
    assert not summary['account_quota_used'] and summary['mode'] == 'web_research_api_model'
    store.resume_job(job['id'])
    second = (await runner.run(job['id']))['summary']
    assert second['answer_reused'] and second['model_used']
    assert not second['api_key_used'] and not second['model_used_this_run']
    assert second['api_requests_attempted'] == instances[-1].requests_attempted == 0
    ApiModel.revision = 'v2'
    store.resume_job(job['id'])
    third = (await runner.run(job['id']))['summary']
    assert not third['answer_reused'] and third['api_requests_completed'] == 2
    assert third['answer'] == first['summary']['answer']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['answer.model', 'research.answer'])
async def test_missing_api_profile_keeps_evidence_and_reports_no_usage(tmp_path, monkeypatch, kind):
    def unavailable(root, backend, *, api_profile):
        assert backend == 'api' and api_profile == 'missing'
        raise LocalModelUnavailable('Configured API profile unavailable')
    if kind == 'answer.model':
        monkeypatch.setattr(analysis, 'text_client', unavailable)
        store, runner, _ = stored_runtime(tmp_path, [])
        runner.handlers[kind] = analysis.answer_model
    else:
        monkeypatch.setattr(discovery, 'text_client', unavailable)
        handler = partial(discovery.research_answer, search_handler=found,
                          source_handler=partial(discovery.fetch_sources, session_factory=SourceSession))
        store, runner = runtime(tmp_path, handler, kind)
    result = await runner.run(store.create_job(kind, {'query': 'telescope', 'fetch_top': 1,
        'answer_backend': 'api', 'api_profile': 'missing'})['id'])
    assert result['status'] == 'partial', result
    summary = result['summary']
    assert summary['answer_status'] == 'api_model_unavailable' and summary['evidence']
    assert summary['api_selected'] and not summary['api_key_used'] and not summary['model_used_this_run']
    assert not summary['account_quota_used'] and summary['answer'] is None


@pytest.mark.asyncio
async def test_no_sources_never_creates_api_client(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No model is needed for empty source evidence')
    async def empty(ctx):
        return {'status': 'complete', 'query': 'absent', 'results': []}
    monkeypatch.setattr(discovery, 'text_client', forbidden)
    store, runner = runtime(tmp_path, partial(discovery.research_answer, search_handler=empty), 'research.answer')
    result = await runner.run(store.create_job('research.answer', {'query': 'absent',
        'answer_backend': 'api', 'api_profile': 'fixture'})['id'])
    summary = result['summary']
    assert summary['answer_status'] == 'no_fetched_evidence'
    assert summary['api_selected'] and not summary['api_key_used']
    assert summary['api_requests_attempted'] == 0


@pytest.mark.asyncio
async def test_api_rewrite_failure_is_partial_and_reports_attempt_not_completion(tmp_path, monkeypatch):
    class Failing(ApiModel):
        async def complete(self, *args, **kwargs):
            self.requests_attempted += 1
            raise LocalModelUnavailable('API request unavailable')
    monkeypatch.setattr(analysis, 'text_client', lambda root, backend, **kw: Failing(root))
    store, runner, _ = stored_runtime(tmp_path, [])
    runner.handlers['answer.model'] = analysis.answer_model
    result = await runner.run(store.create_job('answer.model', {'query': 'absent phrase',
        'answer_backend': 'api', 'api_profile': 'fixture'})['id'])
    summary = result['summary']
    assert result['status'] == 'partial' and summary['answer_status'] == 'api_model_unavailable'
    assert summary['api_key_used'] and summary['api_requests_attempted'] == 1
    assert not summary['model_used_this_run'] and summary['api_requests_completed'] == 0


def test_deadline_snapshot_retains_claims_and_actual_api_use(tmp_path):
    paths = AppPaths.discover(tmp_path)
    context = JobContext('fixture', {'answer_backend': 'api', 'api_profile': 'fixture'},
        Store(paths.database), paths, AsyncExitStack())
    context.answer_progress = {'status': 'complete', 'claims': [{'text': 'Reviewed claim'}],
                               'model_used': True}
    context.answer_model_client = SimpleNamespace(requests_attempted=2, requests_completed=1)
    snapshot = context.answer_budget_summary('time_budget')
    assert snapshot['status'] == 'partial' and snapshot['claims']
    assert snapshot['api_key_used'] and snapshot['api_requests_attempted'] == 2
    assert snapshot['api_requests_completed'] == 1 and not snapshot['account_quota_used']
    assert context.answer_progress['status'] == 'complete'


@pytest.mark.asyncio
async def test_exhausted_api_checkpoint_cannot_bypass_identity_or_usage_on_resume(tmp_path, monkeypatch):
    """A deadline after final checkpoint can leave an exhausted stream resumable."""
    for kind in ('research.answer', 'answer.model'):
        instances = []
        def factory(root, backend, *, api_profile):
            instance = ApiModel(root)
            instances.append(instance)
            return instance
        module = discovery if kind == 'research.answer' else analysis
        monkeypatch.setattr(module, 'text_client', factory)
        async def complete_search(ctx):
            return {**await found(ctx), 'status': 'complete'}
        inner = partial(discovery.research_answer, search_handler=complete_search,
            source_handler=partial(discovery.fetch_sources, session_factory=SourceSession)) if kind == 'research.answer' else analysis.answer_model
        async def after_checkpoint(ctx):
            result = await inner(ctx)
            assert result['status'] == 'complete'
            raise BudgetExceeded('time_budget')
        store, runner = runtime(tmp_path / kind, after_checkpoint, kind)
        if kind == 'answer.model':
            seed = store.create_job('fixture', {})
            store.transition(seed['id'], 'running')
            store.commit_page(seed['id'], 'page', [Record('web', 'page', 'seed', 'https://source.test/report',
                {'title': 'Telescope report', 'text': 'The telescope observed a distant star in 2025.'})],
                next_cursor=None, exhausted=True)
            store.transition(seed['id'], 'complete')
        ApiModel.revision = 'v1'
        job = store.create_job(kind, {'query': 'telescope', 'fetch_top': 1,
            'answer_backend': 'api', 'api_profile': 'fixture'})
        first = await runner.run(job['id'])
        stream = 'discovery_answer' if kind == 'research.answer' else 'model_answer'
        assert first['status'] == 'partial' and store.checkpoint(job['id'], stream)['exhausted']
        store.resume_job(job['id'])
        second = await runner.run(job['id'])
        assert len(instances) == 2, 'API profile must be resolved even with an exhausted checkpoint'
        expected = 0 if kind == 'research.answer' else 2
        assert second['summary']['api_requests_attempted'] == expected
        assert second['summary']['api_key_used'] is bool(expected)
        ApiModel.revision = 'v2'
        store.resume_job(job['id'])
        third = await runner.run(job['id'])
        assert third['summary']['api_requests_attempted'] == 2
        if kind == 'answer.model':
            assert store.checkpoint(job['id'], stream)['metadata']['api_model_identity']['revision'] == 'v2'
