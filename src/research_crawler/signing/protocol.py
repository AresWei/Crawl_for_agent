from __future__ import annotations

import json
from urllib.parse import urlsplit

VERSION = 1
MAX_BYTES = 65536
CAPABILITIES = {'xhs': ['headers'], 'dy': ['a_bogus'], 'zhihu': ['headers'],
                'ks': 'requires_browser'}


class SigningError(RuntimeError):
    pass


def validate(value):
    if not isinstance(value, dict) or set(value) != {'version', 'platform', 'operation', 'payload'}:
        raise SigningError('invalid_request')
    if type(value['version']) is not int or value['version'] != VERSION:
        raise SigningError('unsupported_version')
    platform, operation, data = value['platform'], value['operation'], value['payload']
    if platform == 'ks':
        raise SigningError('requires_browser')
    if platform not in ('xhs', 'dy', 'zhihu') or operation not in CAPABILITIES[platform]:
        raise SigningError('unsupported_operation')
    if not isinstance(data, dict):
        raise SigningError('invalid_payload')
    fields = {'xhs': {'path', 'method', 'cookies', 'values'},
              'dy': {'path', 'query', 'ua'}, 'zhihu': {'path', 'dc0'}}[platform]
    if set(data) != fields:
        raise SigningError('invalid_fields')
    if any(not isinstance(v, str) for k, v in data.items() if k != 'values'):
        raise SigningError('invalid_field_type')
    if not data['path'].startswith('/') or data['path'].startswith('//'):
        raise SigningError('relative_platform_path_required')
    if platform == 'xhs' and (data['method'] not in ('GET', 'POST') or not isinstance(data['values'], dict)):
        raise SigningError('unsupported_method_or_values')
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BYTES:
        raise SigningError('request_too_large')
    return value


def service_url(value):
    url = urlsplit(value)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment or not url.port):
        raise SigningError('loopback_service_url_required')
    return value.rstrip('/')
