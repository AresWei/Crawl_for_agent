"""Local profile configuration only: no completion, network, or real API key."""
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from research_crawler.core.jobs import JobRunner
from research_crawler.core.model_profiles import ModelProfileStore
from research_crawler.core.paths import AppPaths
from research_crawler.core.store import Store
from research_crawler.service import create_app


def app(tmp_path):
    paths = AppPaths(tmp_path)
    paths.initialize()
    return create_app(tmp_path, runner_override=JobRunner(Store(paths.database), paths))


def test_config_roundtrip_auth_status_rotation_and_no_model_call(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('Saving or listing profiles must not make network requests')
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    payload = {'provider': 'openai', 'base_url': 'https://example.invalid/v1',
               'model': 'fixture-model', 'api_key': 'fake-key-DO-NOT-RETURN'}
    with TestClient(app(tmp_path)) as client:
        for method, path in [('GET', '/api/model-profiles'), ('PUT', '/api/model-profiles/sample'), ('DELETE', '/api/model-profiles/sample')]:
            assert client.request(method, path, json=payload).status_code == 401
        headers = {'X-Research-Token': (tmp_path / 'state/api-token').read_text()}
        client.headers.update(headers)
        assert client.get('/api/status').json()['model_api']['configured'] is False
        assert client.put('/api/model-profiles/sample', headers={'Origin': 'https://unrelated.invalid'}, json=payload).status_code == 403
        saved = client.put('/api/model-profiles/sample', json=payload)
        assert saved.status_code == 200
        revision = saved.json()['revision']
        listed = client.get('/api/model-profiles')
        status = client.get('/api/status')
        assert listed.json()['profiles'] == [saved.json()]
        assert status.json()['model_api']['configured'] is True
        assert status.json()['model_api']['explicit_selection_required'] is True
        assert status.json()['paid_api_enabled'] is True
        assert status.json()['active'] == [] and client.get('/api/jobs').json()['jobs'] == []
        assert ModelProfileStore(tmp_path).load('sample')['api_key'] == payload['api_key']
        for response in (saved, listed, status):
            assert payload['api_key'] not in response.text and 'api_key' not in response.text
        payload.update(provider='anthropic', model='fixture-second', api_key='rotated-key-DO-NOT-RETURN')
        rotated = client.put('/api/model-profiles/sample', json=payload)
        assert rotated.status_code == 200 and rotated.json()['revision'] != revision
        assert rotated.json()['provider'] == 'anthropic'
        assert payload['api_key'] not in rotated.text
        assert client.delete('/api/model-profiles/sample').json() == {'deleted': True}
        assert client.get('/api/model-profiles').json() == {'profiles': []}
        assert client.get('/api/status').json()['paid_api_enabled'] is False


def test_validation_never_echoes_secret_body_or_builds_task_history(tmp_path):
    secret = 'fake-sensitive-KEY-123'
    good = {'provider': 'openai', 'base_url': 'https://example.invalid/v1', 'model': 'fixture', 'api_key': secret}
    with TestClient(app(tmp_path)) as client:
        client.headers.update({'X-Research-Token': (tmp_path / 'state/api-token').read_text()})
        invalid = [dict(good, provider=secret), dict(good, api_key={'nested': secret}),
                   dict(good, base_url=f'https://user:{secret}@example.invalid'), dict(good, extra=secret)]
        for body in invalid:
            result = client.put('/api/model-profiles/sample', json=body)
            assert result.status_code == 422 and secret not in result.text
            assert 'input' not in result.json()
        broken = client.put('/api/model-profiles/sample', content='{"api_key":"' + secret)
        assert broken.status_code == 422 and secret not in broken.text
        large = client.put('/api/model-profiles/sample', content=secret * 1000)
        assert large.status_code == 413 and secret not in large.text
        assert client.get('/api/model-profiles').json() == {'profiles': []}
        assert client.get('/api/jobs').json()['jobs'] == []


def test_actual_ui_answer_helper_keeps_api_opt_in_and_profile_scoped():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for actual UI helper execution')
    source = (Path(__file__).parents[1] / 'src/research_crawler/ui/app.js').read_text(encoding='utf-8')
    helper = source[source.index('function answerBackendParams('):source.index('function imageCreationParams(')]
    script = "const fields={'answer-backend':{value:'api'},'api-profile':{value:'selected'}}; const $=id=>fields[id];\n" + helper + """
const research=answerBackendParams('research'), model=answerBackendParams('model');
fields['answer-backend'].value='codex';const codex=answerBackendParams('research');
fields['answer-backend'].value='local';const local=answerBackendParams('research');
console.log(JSON.stringify({research,model,codex,local,web:answerBackendParams('web')}));
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True, timeout=10)
    actual = json.loads(result.stdout)
    assert actual['research'] == {'answer_backend': 'api', 'api_profile': 'selected', 'evidence_selection': 'context'}
    assert actual['model'] == {'answer_backend': 'api', 'api_profile': 'selected'}
    assert actual['codex'] == {'answer_backend': 'codex', 'evidence_selection': 'context'}
    assert actual['local'] == {'answer_backend': 'local', 'evidence_selection': 'planned'}
    assert actual['web'] == {}


def test_invalid_api_job_parameters_return_safe_422_before_persistence(tmp_path):
    paths = AppPaths(tmp_path)
    paths.initialize()
    store = Store(paths.database)
    runner = JobRunner(store, paths)
    async def must_not_run(context):
        pytest.fail('Rejected API parameters must not start a task')
    runner.register('research.answer', must_not_run)
    secret = 'fake-job-secret-DO-NOT-ECHO'
    invalid = [
        {'answer_backend': 'api', 'question': 'Example'},
        {'answer_backend': secret, 'question': 'Example'},
        {'answer_backend': 'api', 'api_profile': 'sample', 'question': 'Example',
         'nested': {'api_key': secret}},
    ]
    with TestClient(create_app(tmp_path, runner_override=runner)) as client:
        client.headers.update({'X-Research-Token': (tmp_path / 'state/api-token').read_text()})
        for params in invalid:
            response = client.post('/api/jobs', json={'kind': 'research.answer', 'params': params})
            assert response.status_code == 422
            assert secret not in response.text and 'api_key' not in response.text
            assert client.get('/api/jobs').json()['jobs'] == []
