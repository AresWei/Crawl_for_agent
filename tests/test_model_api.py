"""Explicit API fixture coverage: MockTransport only, no account or billed requests."""
import asyncio
import json
import os

import httpx
import pytest

from research_crawler.core.model_profiles import ModelProfileStore
from research_crawler.intelligence.api_model import ApiModelClient, ApiModelUnavailable
from research_crawler.intelligence.text_client import text_client

KEY='fixture-private-credential-not-real'
MESSAGES=[{'role':'system','content':'Be precise.'},{'role':'user','content':'Return a number.'}]


def save(root,provider='openai',url='https://provider.example/v1'):
    return ModelProfileStore(root).save('chosen',provider=provider,base_url=url,model='fixture-model',api_key=KEY)


async def fake_client(root,handler):
    client=ApiModelClient(root,'chosen')
    await client._http.aclose()
    client._http=httpx.AsyncClient(transport=httpx.MockTransport(handler),trust_env=False,follow_redirects=False)
    return client


def test_private_profile_public_metadata_revision_and_delete(tmp_path):
    first=save(tmp_path);store=ModelProfileStore(tmp_path)
    assert KEY not in json.dumps(first) and KEY not in json.dumps(store.list())
    assert store.load('chosen')['api_key']==KEY
    private=tmp_path/'state/model-api/chosen.json'
    if os.name=='nt':assert private.read_bytes().startswith(b'DPAPI1\n') and KEY.encode() not in private.read_bytes()
    second=save(tmp_path)
    assert first['revision']!=second['revision'] and len(store.list())==1
    store.delete('chosen');store.delete('chosen');assert store.list()==[]


@pytest.mark.parametrize('url',['http://remote.example/v1','https://user:pass@example.com/v1',
    'https://example.com/v1?key=hidden','https://example.com/#hidden','file:///tmp/model','https://example.com\\@evil.test'])
def test_endpoint_rejects_insecure_or_secret_bearing_urls(tmp_path,url):
    with pytest.raises(ValueError):save(tmp_path,url=url)
    assert not (tmp_path/'state').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,reason,expected',[('openai','stop','stop'),('openai','length','length'),
    ('anthropic','end_turn','stop'),('anthropic','max_tokens','length')])
async def test_protocol_and_honest_finish(tmp_path,provider,reason,expected):
    info=save(tmp_path,provider,'http://127.0.0.1:9876/api/v3')
    calls=[]
    def handler(request):
        calls.append(request);body=json.loads(request.content)
        assert body['model']=='fixture-model' and body['stream'] is False
        if provider=='anthropic':
            assert request.url.path=='/api/v3/messages'
            assert request.headers['x-api-key']==KEY and request.headers['anthropic-version']=='2023-06-01'
            assert 'JSON schema' in body['system'] and all(m['role']!='system' for m in body['messages'])
            return httpx.Response(200,json={'content':[{'type':'text','text':'{"n":1}'}],'stop_reason':reason,'usage':{'input_tokens':3,'output_tokens':4,'secret':KEY}})
        assert request.url.path=='/api/v3/chat/completions' and request.headers['Authorization']=='Bearer '+KEY
        assert 'JSON schema' in body['messages'][0]['content']
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"n":1}'},'finish_reason':reason}],
            'usage':{'prompt_tokens':3,'completion_tokens':4,'secret':KEY}})
    async with await fake_client(tmp_path,handler) as client:
        assert client.model_calls==0 and not client.api_key_used
        assert (await client.health())['verified_remote'] is False and calls==[]
        result=await client.complete(MESSAGES,response_format={'type':'json_schema','json_schema':{'schema':{'type':'object'}}})
        assert result.finish_reason==expected and result.text=='{"n":1}'
        assert client.model_calls==1 and client.api_key_used
        assert client.requests_attempted==client.requests_completed==1
        assert client.cache_identity['revision']==info['revision']
        assert KEY not in repr(result) and KEY not in repr(client.cache_identity)
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,reason',[('openai','tool_calls'),('openai','unknown'),
    ('openai','content_filter'),('anthropic','tool_use'),('anthropic','stop_sequence')])
async def test_valid_json_with_incomplete_finish_cannot_be_published(tmp_path,provider,reason):
    save(tmp_path,provider)
    def handler(request):
        body=({'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':reason}]}
              if provider=='openai' else {'content':[{'type':'text','text':'{"ok":true}'}],'stop_reason':reason})
        return httpx.Response(200,json=body)
    async with await fake_client(tmp_path,handler) as client:
        with pytest.raises(ApiModelUnavailable,match='did not complete'):
            await client.complete(MESSAGES)
        assert client.requests_attempted==1 and client.requests_completed==0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['redirect','status','invalid','timeout'])
async def test_no_retry_redirect_or_secret_errors(tmp_path,failure):
    save(tmp_path);calls=[]
    def handler(request):
        calls.append(request)
        if failure=='timeout':raise httpx.ReadTimeout(KEY,request=request)
        if failure=='redirect':return httpx.Response(307,headers={'location':'https://other.example/'+KEY})
        if failure=='status':return httpx.Response(401,text=KEY)
        return httpx.Response(200,json={'error':KEY})
    async with await fake_client(tmp_path,handler) as client:
        with pytest.raises(ApiModelUnavailable) as error:await client.complete(MESSAGES)
        assert KEY not in str(error.value)
        assert client.requests_attempted==1 and client.requests_completed==0
    assert len(calls)==1


@pytest.mark.asyncio
async def test_input_schema_budget_cancellation_and_explicit_selection(tmp_path):
    save(tmp_path);calls=[]
    async def handler(request):
        calls.append(request);raise asyncio.CancelledError()
    with pytest.raises(ValueError):text_client(tmp_path,'api')
    with pytest.raises(ValueError):text_client(tmp_path,'local',api_profile='chosen')
    async with await fake_client(tmp_path,handler) as client:
        schema={'type':'string','description':'x'*96000}
        with pytest.raises(ValueError):await client.complete(MESSAGES,response_format={'type':'json_schema','json_schema':{'schema':schema}})
        assert calls==[] and not client.api_key_used
        with pytest.raises(asyncio.CancelledError):await client.complete(MESSAGES)
        assert len(calls)==1
