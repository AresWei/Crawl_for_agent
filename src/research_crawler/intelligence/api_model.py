"""Explicit, separately billed model APIs; never an automatic fallback."""
import asyncio
import json
import math
import time

import httpx

from ..core.model_profiles import ModelProfileStore
from .local_model import LocalModelUnavailable, ModelCompletion


class ApiModelUnavailable(LocalModelUnavailable):
    """Sanitized provider failure with no response body or request credentials."""


class ApiModelClient:
    max_input_chars=96000

    def __init__(self,root,profile,*,timeout=120.0):
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 1<=timeout<=300:
            raise ValueError('API timeout must be 1..300 seconds')
        try:self._profile=ModelProfileStore(root).load(profile)
        except (OSError,ValueError,KeyError,RuntimeError):
            raise ApiModelUnavailable('Model API profile is missing or unavailable') from None
        self.timeout=timeout
        self.model_calls=0
        self.requests_attempted=0
        self.requests_completed=0
        self.api_key_used=False
        self.cache_identity={key:self._profile[key] for key in ('name','provider','model','revision')}
        self.cache_identity['model_id']=self._profile['model']
        self._http=httpx.AsyncClient(trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(timeout,connect=min(timeout,10)),
            limits=httpx.Limits(max_connections=2,max_keepalive_connections=1))

    async def __aenter__(self):return self
    async def __aexit__(self,*exc):await self.aclose()
    async def aclose(self):await self._http.aclose()

    async def health(self):
        return {'available':True,'configured':True,'status':'configured_unverified',
                'provider':self._profile['provider'],'model_id':self._profile['model'],
                'cache_identity':self.cache_identity,'network_required':True,'paid_api':True,
                'verified_remote':False}

    async def complete(self,messages,*,max_tokens=512,temperature=0.2,response_format=None):
        if type(max_tokens) is not int or not 1<=max_tokens<=16384:raise ValueError('max_tokens must be 1..16384')
        if isinstance(temperature,bool) or not isinstance(temperature,(float,int)) or not math.isfinite(temperature) or not 0<=temperature<=1:
            raise ValueError('API temperature must be between 0 and 1')
        if not isinstance(messages,list) or not 1<=len(messages)<=64 or any(
                not isinstance(m,dict) or m.get('role') not in {'system','user','assistant'} or not isinstance(m.get('content'),str) for m in messages):
            raise ValueError('Expected 1..64 text messages with supported roles')
        outgoing=[{'role':m['role'],'content':m['content']} for m in messages]
        # Portable schema prompting avoids retrying/billing an unsupported native format.
        # Callers must still validate JSON/schema and semantic support independently.
        if response_format is not None:
            if not isinstance(response_format,dict) or response_format.get('type') not in {'json_object','json_schema'}:
                raise ValueError('Unsupported response format')
            instruction='Return only valid JSON, without markdown fences.'
            if response_format['type']=='json_schema':
                schema=response_format.get('json_schema',{}).get('schema')
                if not isinstance(schema,dict):raise ValueError('Expected JSON schema object')
                instruction+=' Follow this JSON schema: '+json.dumps(schema,ensure_ascii=False,allow_nan=False)
            outgoing.insert(0,{'role':'system','content':instruction})
        if sum(len(m['content']) for m in outgoing)>self.max_input_chars:raise ValueError('API input exceeds character budget')
        p=self._profile
        base=p['base_url']
        payload={'model':p['model'],'max_tokens':max_tokens,'temperature':temperature,'stream':False}
        if p['provider']=='anthropic':
            endpoint=base+'/messages'
            headers={'x-api-key':p['api_key'],'anthropic-version':'2023-06-01'}
            payload['system']='\n\n'.join(m['content'] for m in outgoing if m['role']=='system')
            payload['messages']=[m for m in outgoing if m['role']!='system']
            if not payload['messages']:raise ValueError('Anthropic requires a non-system message')
        else:
            endpoint=base+'/chat/completions'
            headers={'Authorization':'Bearer '+p['api_key']}
            payload['messages']=outgoing
        started=time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout):
                self.model_calls+=1
                self.requests_attempted+=1
                self.api_key_used=True
                async with self._http.stream('POST',endpoint,headers=headers,json=payload) as response:
                    if not 200<=response.status_code<300:
                        raise ApiModelUnavailable(f'Model API returned HTTP {response.status_code}')
                    body=bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body)>2_000_000:raise ApiModelUnavailable('Model API response exceeds byte budget')
            data=json.loads(body)
            if p['provider']=='anthropic':
                blocks=data['content']
                text=''.join(b['text'] for b in blocks if b.get('type')=='text')
                reason=data.get('stop_reason','unknown')
                finish='stop' if reason=='end_turn' and all(b.get('type')=='text' for b in blocks) else ('length' if reason=='max_tokens' else reason)
            else:
                choice=data['choices'][0];message=choice['message'];text=message.get('content')
                finish=choice.get('finish_reason') or 'unknown'
                if finish=='stop' and (message.get('tool_calls') or message.get('refusal')):finish='incomplete'
            if not isinstance(text,str) or not text.strip():raise ApiModelUnavailable('Model API returned no answer text')
            if finish not in {'stop','length','tool_calls','tool_use','content_filter','refusal','incomplete','pause_turn','stop_sequence'}:
                finish='unknown'
            if finish not in {'stop','length'}:
                raise ApiModelUnavailable('Model API did not complete an answer')
            # Only numeric usage counters may enter persisted receipts; never raw provider metadata.
            usage={k:v for k,v in data.get('usage',{}).items() if k in {
                'prompt_tokens','completion_tokens','total_tokens','input_tokens','output_tokens',
                'cache_read_input_tokens','cache_creation_input_tokens'} and type(v) is int and v>=0}
        except ApiModelUnavailable:raise
        except (httpx.HTTPError,TimeoutError,ValueError,KeyError,TypeError,IndexError,AttributeError):
            raise ApiModelUnavailable('Model API request failed or returned an invalid response') from None
        self.requests_completed+=1
        return ModelCompletion(text.strip(),p['model'],usage,round((time.perf_counter()-started)*1000,3),finish)
