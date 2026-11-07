"""Selected-account remote session probes adapted from MediaCrawler pong methods.

Copyright (c) 2025 relakkes@gmail.com. NON-COMMERCIAL LEARNING LICENSE 1.1.
Frozen source54a3d92412f4b20c06b45f1d542b78dd92049d6d; shared core owns
credentials/HTTP/budgets. No response body or personal identity leaves this module.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from ..core.accounts import AccountStore
from ..core.jobs import NeedsLogin
from ..core.records import utcnow
from ..core.sessions import open_http
from .xiaohongshu_support import UA


PLATFORMS=frozenset({'xhs','dy','ks','bili','wb','tieba','zhihu'})
ENDPOINTS={
    'bili':'https://api.bilibili.com/x/web-interface/nav',
    'wb':'https://m.weibo.cn/api/config',
    'xhs':'https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo',
    'zhihu':'https://www.zhihu.com/api/v4/me',
    'ks':'https://www.kuaishou.com/graphql',
}
MAX_RESPONSE_BYTES=262144


def interpret(platform,body):
    """Interpret explicit source authentication indicators, never mere HTTP200."""
    if not isinstance(body,dict):return 'indeterminate','unexpected_response_shape'
    data=body.get('data')
    if platform=='bili':
        if body.get('code')==-101:return 'auth_required','platform_reports_logged_out'
        flag=data.get('isLogin') if isinstance(data,dict) else None
        if type(body.get('code')) is int and body['code']==0 and type(flag) is bool:
            return ('valid','remote_login_indicator') if flag else ('auth_required','platform_reports_logged_out')
    elif platform=='wb':
        flag=data.get('login') if isinstance(data,dict) else None
        if type(body.get('ok')) is int and body['ok']==1 and type(flag) is bool:
            return ('valid','remote_login_indicator') if flag else ('auth_required','platform_reports_logged_out')
    elif platform=='xhs':
        if body.get('code') in {-100,-101,300011}:return 'auth_required','platform_reports_logged_out'
        result=data.get('result') if isinstance(data,dict) else None
        if isinstance(result,dict) and result.get('success') is True:
            return 'indeterminate','upstream_success_not_auth_proof'
    elif platform=='zhihu':
        error=body.get('error')
        if isinstance(error,dict) and error.get('code') in {100,401}:
            return 'auth_required','platform_reports_logged_out'
        if not error and isinstance(body.get('uid'),(str,int)) and body.get('uid') and isinstance(body.get('name'),str) and body['name']:
            return 'valid','remote_self_identity_present'
    elif platform=='ks':
        profile=data.get('visionProfileUserList') if isinstance(data,dict) else None
        if not body.get('errors') and isinstance(profile,dict) and type(profile.get('result')) is int and profile['result']==1:
            return 'indeterminate','upstream_success_not_auth_proof'
    return 'indeterminate','authentication_indicator_missing'


def _result(platform,state,reason,method,*,http_status=None,network_used=False):
    return {'platform':platform,'state':state,'reason_code':reason,'method':method,
            'checked_at':utcnow(),'http_status':http_status,'network_used':network_used,
            'scope':'selected_saved_account','credential_values_recorded':False,
            'validity_scope':'response_at_check_time_only' if state=='valid' else 'not_verified'}


async def validate_session(context,platform,account,*,transport=None,signer=None):
    """Read-only one-probe check; transport/signer injection is for fixtures only.

    Returns valid/auth_required/indeterminate; does not mutate account metadata.
    Pause/cancel and shared budget exceptions propagate to the caller. The caller
    must bind saved results to the same account-state revision it selected.
    """
    if platform not in PLATFORMS:raise ValueError('Unsupported session-validation platform')
    if not isinstance(account,str) or not account:raise ValueError('Select an account to validate')
    method='source_local_indicators_only' if platform in {'dy','tieba'} else 'source_remote_auth_probe'
    store=AccountStore(context.paths)
    try:
        store.state(platform,account,required=True)
    except NeedsLogin:
        return _result(platform,'auth_required','saved_state_missing_or_expired',method)
    if platform in {'dy','tieba'}:
        # Frozen upstream only checks cookie/localStorage presence. That is not
        # server acceptance and cannot be upgraded into remote login validity.
        return _result(platform,'indeterminate','source_has_no_remote_auth_check',method)
    timeout=context.params.get('validation_timeout_seconds',20)
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not 1<=timeout<=60:
        raise ValueError('validation_timeout_seconds must be1..60')
    endpoint=ENDPOINTS[platform]
    if platform=='xhs' and context.params.get('international'):
        endpoint='https://webapi.rednote.com/api/sns/web/v1/user/selfinfo'
    http_status=None;network_used=False
    try:
        async with asyncio.timeout(timeout):
            http=await open_http(context,platform=platform,account=account,require_account=True,
                timeout=timeout,transport=transport,headers={'User-Agent':UA,'Accept':'application/json',
                    'Referer':'https://www.bilibili.com/' if platform=='bili' else 'https://'+urlsplit(endpoint).hostname+'/'})
            cookies=http.build_request('GET',endpoint).headers.get('cookie','')
            if not cookies:
                return _result(platform,'indeterminate','selected_state_has_no_endpoint_cookies',method)
            headers={};kwargs={};verb='GET';path=urlsplit(endpoint).path
            if platform=='xhs':
                if signer is None:
                    from xhshow import Xhshow
                    signer=Xhshow()
                headers=signer.sign_headers_get(uri=path,cookies=cookies,params={})
                if not headers.get('x-s') or not headers.get('x-t'):
                    return _result(platform,'indeterminate','signer_unavailable',method)
            elif platform=='zhihu':
                if signer is None:
                    from .zhihu import ZhihuClient
                    signer=ZhihuClient(http,context,'zhihu',interval=0)
                headers=await signer.signed_headers(path)
            elif platform=='ks':
                verb='POST'
                # The source query requests following identities unnecessarily;
                # retain only its authentication result field.
                kwargs['json']={'operationName':'visionProfileUserList','variables':{'ftype':1},
                    'query':'query visionProfileUserList($ftype: Int) { visionProfileUserList(ftype: $ftype) { result } }'}
            request=http.build_request(verb,endpoint,headers=headers,**kwargs)
            network_used=True
            response=await http.send(request,stream=True,follow_redirects=False)
            try:
                http_status=response.status_code
                if response.headers.get('cf-mitigated')=='challenge' or http_status in {403,429,412,418}:
                    return _result(platform,'indeterminate','access_challenge_or_rate_limit',method,http_status=http_status,network_used=True)
                if http_status==401:
                    return _result(platform,'auth_required','http_unauthorized',method,http_status=http_status,network_used=True)
                if http_status!=200:
                    return _result(platform,'indeterminate','unexpected_http_status',method,http_status=http_status,network_used=True)
                data=bytearray()
                async for chunk in response.aiter_bytes():
                    await context.check_control();data.extend(chunk)
                    if len(data)>MAX_RESPONSE_BYTES:
                        return _result(platform,'indeterminate','response_byte_limit',method,http_status=http_status,network_used=True)
                body=json.loads(bytes(data))
                state,reason=interpret(platform,body)
                return _result(platform,state,reason,method,http_status=http_status,network_used=True)
            finally:
                await response.aclose()
    except NeedsLogin:
        # Signing cookies can be absent while other saved state remains. This
        # is insufficient to establish a remote logout or invalidate the account.
        return _result(platform,'indeterminate','signing_state_unavailable',method,http_status=http_status,network_used=network_used)
    except (httpx.HTTPError,TimeoutError):
        return _result(platform,'indeterminate','network_unavailable',method,http_status=http_status,network_used=network_used)
    except (ValueError,TypeError,ImportError,RuntimeError,KeyError):
        # Never include exception text: it may embed signed URLs or response data.
        return _result(platform,'indeterminate','probe_unavailable_or_malformed_response',method,http_status=http_status,network_used=network_used)
