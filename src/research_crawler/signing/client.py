from __future__ import annotations

import httpx
from .protocol import VERSION, MAX_BYTES, SigningError, service_url, validate


def backend(context):
    value = context.params.get('signing_backend', 'local')
    if value not in ('local', 'service'):
        raise SigningError('unsupported_signing_backend')
    return value


async def service_sign(context, platform, operation, payload):
    request = validate(dict(version=VERSION, platform=platform, operation=operation, payload=payload))
    url = service_url(context.params.get('signing_service_url', 'http://127.0.0.1:8950'))
    try:
        token = (context.paths.root / 'state/signing/token').read_text().strip()
        if len(token) < 32:
            raise SigningError('service_token_invalid')
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20) as http:
            async with http.stream('POST', url + '/v1/sign', json=request,
                                   headers={'Authorization': 'Bearer ' + token}) as response:
                if response.status_code != 200:
                    raise SigningError('service_rejected_' + str(response.status_code))
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise SigningError('service_response_too_large')
        import json
        result = json.loads(body)
        if result.get('version') != VERSION or result.get('platform') != platform or not isinstance(result.get('result'), dict):
            raise SigningError('invalid_service_response')
        return result['result']
    except (OSError, httpx.HTTPError, ValueError):
        raise SigningError('signing_service_unavailable') from None
