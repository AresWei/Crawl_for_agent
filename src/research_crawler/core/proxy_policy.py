"""Named proxy rotation adapted from Crawl4AI RoundRobinProxyStrategy.

Frozen838869bdda93f25e6d50b3f50ae9577d9b19a586, Apache-2.0 and additional
attribution in licenses/crawl4ai. The shared core owns transport and credentials.
This policy never fetches, retries or reacts to a block/challenge response.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from urllib.parse import urlsplit

from .paths import safe_name


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class ProxyPolicy:
    """Choose once per logical source; persist dump with that source's checkpoint.

    select() returns only an alias already present in the caller's supplied
    registry. Repeated calls for one source_id are idempotent through expiry and
    resume. release_source() is optional after an atomic result commit. A saved
    account, explicit browser session or persistent profile stays on one proxy
    for this job. Anonymous sticky mode adds origin-based TTL bindings, adapted
    from upstream's explicit session-id bindings.
    """
    MAX_BINDINGS=2048

    def __init__(self,params,snapshot=None,*,available_names,clock=time.time):
        if not isinstance(params,dict):raise ValueError('Proxy policy parameters must be an object')
        config=params.get('proxy_policy')
        if not isinstance(config,dict) or set(config)-{'names','strategy','sticky_ttl_seconds'}:
            raise ValueError('proxy_policy accepts names, strategy and sticky_ttl_seconds')
        names=config.get('names')
        if not isinstance(names,list) or not 1<=len(names)<=32 or any(not isinstance(name,str) for name in names):
            raise ValueError('proxy_policy names must contain1..32 supplied proxy aliases')
        self.names=tuple(safe_name(name) for name in names)
        if len(set(self.names))!=len(self.names):raise ValueError('Proxy aliases must be distinct')
        known=set(available_names)
        if any(name not in known for name in self.names):raise ValueError('Proxy policy references an unknown supplied alias')
        if params.get('proxy_id') is not None:raise ValueError('Choose proxy_id or proxy_policy, not both')
        if params.get('cdp_url'):raise ValueError('An attached browser retains its existing proxy configuration')
        self.strategy=config.get('strategy','round_robin')
        if self.strategy not in {'round_robin','sticky'}:raise ValueError('Unknown proxy strategy')
        ttl=config.get('sticky_ttl_seconds')
        if ttl is not None and (isinstance(ttl,bool) or not isinstance(ttl,(int,float)) or not math.isfinite(ttl) or not 0<=ttl<=86400):
            raise ValueError('sticky_ttl_seconds must be null or0..86400')
        if self.strategy!='sticky' and ttl is not None:raise ValueError('TTL requires sticky proxy strategy')
        self.ttl=ttl
        self.params={key:params.get(key) for key in ('account','session_id','browser_profile')}
        self.clock=clock
        self.identity=_digest({'names':self.names,'strategy':self.strategy,'ttl':ttl})
        self.state={'schema_version':1,'policy_sha256':self.identity,'next_index':0,'sources':{},'sticky':{},'pinned':{}}
        if snapshot is not None:self._restore(snapshot)

    def _now(self):
        now=self.clock()
        if isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now) or now<0:
            raise ValueError('Invalid proxy policy clock')
        return now

    def _restore(self,snapshot):
        if not isinstance(snapshot,dict) or set(snapshot)!=set(self.state) or snapshot.get('schema_version')!=1 or snapshot.get('policy_sha256')!=self.identity:
            raise ValueError('Proxy checkpoint does not match selected policy')
        index=snapshot.get('next_index')
        if type(index) is not int or not 0<=index<len(self.names):raise ValueError('Invalid proxy checkpoint cursor')
        for bucket in ('sources','sticky','pinned'):
            values=snapshot.get(bucket)
            if not isinstance(values,dict) or len(values)>self.MAX_BINDINGS:raise ValueError('Invalid proxy checkpoint bindings')
            for key,entry in values.items():
                if not isinstance(key,str) or len(key)!=64 or any(ch not in '0123456789abcdef' for ch in key):raise ValueError('Invalid proxy scope hash')
                expected={'proxy_id','scope'} if bucket=='sources' else {'proxy_id','created_at','expires_at'} if bucket=='sticky' else {'proxy_id'}
                if not isinstance(entry,dict) or set(entry)!=expected or entry.get('proxy_id') not in self.names:
                    raise ValueError('Invalid proxy checkpoint selection')
                if bucket=='sources' and (not isinstance(entry['scope'],str) or len(entry['scope'])!=64):raise ValueError('Invalid source scope')
                if bucket=='sticky':
                    for field in ('created_at','expires_at'):
                        value=entry[field]
                        if field=='expires_at' and value is None:continue
                        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:raise ValueError('Invalid sticky expiry')
                    if entry['expires_at'] is not None and entry['expires_at']<entry['created_at']:raise ValueError('Invalid sticky lifetime')
        self.state=copy.deepcopy(snapshot)

    def _next(self):
        chosen=self.names[self.state['next_index']]
        self.state['next_index']=(self.state['next_index']+1)%len(self.names)
        return chosen

    @staticmethod
    def _scope_value(value):
        if value is not None and (not isinstance(value,str) or not value or len(value)>2048):raise ValueError('Invalid proxy identity scope')
        return value

    def select(self,url,*,source_id=None,session_id=None,account=None):
        if not isinstance(url,str) or not url or len(url)>16384:raise ValueError('Invalid logical source URL')
        parsed=urlsplit(url)
        if parsed.scheme not in {'http','https','file','raw'} or parsed.scheme in {'http','https'} and not parsed.hostname:
            raise ValueError('Proxy selection requires a supported source URL')
        if parsed.username or parsed.password:raise ValueError('Source URLs cannot contain credentials')
        source_id=url if source_id is None else source_id
        if not isinstance(source_id,str) or not source_id or len(source_id)>16384:raise ValueError('Invalid logical source identifier')
        account=self._scope_value(account if account is not None else self.params['account'])
        session_id=self._scope_value(session_id if session_id is not None else self.params['session_id'])
        profile=self._scope_value(self.params['browser_profile'])
        # Accounts take precedence: two pages/sessions of the same selected
        # account must not silently acquire different network identities.
        pinned=('account',account) if account else ('session',session_id) if session_id else ('profile',profile) if profile else None
        origin=(parsed.scheme,parsed.hostname,(parsed.port or (443 if parsed.scheme=='https' else 80))) if parsed.hostname else (parsed.scheme,source_id)
        scope=_digest(pinned or ('origin',origin))
        key=_digest(source_id)
        saved=self.state['sources'].get(key)
        if saved is not None:
            if saved['scope']!=scope:raise ValueError('Logical source was already assigned to another identity')
            return saved['proxy_id']
        if len(self.state['sources'])>=self.MAX_BINDINGS:raise ValueError('Proxy source binding budget exceeded')
        now=self._now()
        self.cleanup_expired()
        if pinned:
            mapping=self.state['pinned']
            if scope not in mapping:
                if len(mapping)>=self.MAX_BINDINGS:raise ValueError('Proxy identity binding budget exceeded')
                mapping[scope]={'proxy_id':self._next()}
            selected=mapping[scope]['proxy_id']
        elif self.strategy=='sticky':
            mapping=self.state['sticky']
            if scope not in mapping:
                if len(mapping)>=self.MAX_BINDINGS:raise ValueError('Proxy sticky binding budget exceeded')
                mapping[scope]={'proxy_id':self._next(),'created_at':now,'expires_at':None if self.ttl is None else now+self.ttl}
            selected=mapping[scope]['proxy_id']
        else:selected=self._next()
        self.state['sources'][key]={'proxy_id':selected,'scope':scope}
        return selected

    def release_source(self,source_id):
        """Call only after the source result/checkpoint commit is settled."""
        self.state['sources'].pop(_digest(source_id),None)

    def cleanup_expired(self):
        now=self._now();expired=[key for key,value in self.state['sticky'].items() if value['expires_at'] is not None and now>=value['expires_at']]
        for key in expired:del self.state['sticky'][key]
        return len(expired)

    def dump(self):
        return copy.deepcopy(self.state)

    def evidence(self):
        return {'strategy':self.strategy,'supplied_aliases':list(self.names),'sticky_ttl_seconds':self.ttl,
                'policy_sha256':self.identity,'active_sources':len(self.state['sources']),
                'sticky_scopes':len(self.state['sticky']),'pinned_identities':len(self.state['pinned']),
                'response_triggered_rotation':False,'credentials_recorded':False}
