"""Archive/CT selection adapted from Crawl4AI DomainMapper (Apache-2.0).

See LICENSE.crawl4ai. Shared jobs replace the original independent client and
silent failure handling. Archive pagination follows the providers' public APIs.
"""
from __future__ import annotations
import json
import re
from urllib.parse import urlencode

SERVICE_HOSTS = {'cc': 'index.commoncrawl.org', 'cc_pages': 'index.commoncrawl.org',
                 'cc_catalog': 'index.commoncrawl.org', 'wayback': 'web.archive.org', 'crt': 'crt.sh'}


def cc_endpoint(index, host, *, page=None):
    params = {'url': host, 'matchType': 'host', 'output': 'json',
              'filter': '=status:200', 'collapse': 'urlkey', 'pageSize': 1}
    params.update({'showNumPages': 'true'} if page is None else {'page': page})
    return f'https://index.commoncrawl.org/{index}-index?' + urlencode(params)


def cc_pages(document):
    value = json.loads(document)
    pages = value.get('pages') if isinstance(value, dict) else value
    if isinstance(pages, bool) or not isinstance(pages, int) or not 0 <= pages <= 10_000_000:
        raise ValueError('invalid_common_crawl_page_count')
    return pages


def wayback_endpoint(host, page_size, resume_key=None):
    params = {'url': host, 'matchType': 'host', 'output': 'json',
              'fl': 'original,timestamp,mimetype,statuscode,digest',
              'filter': 'statuscode:200', 'collapse': 'urlkey', 'limit': page_size,
              'showResumeKey': 'true'}
    if resume_key: params['resumeKey'] = resume_key
    return 'https://web.archive.org/cdx/search/cdx?' + urlencode(params)


def parse_wayback(document):
    data = json.loads(document)
    if not isinstance(data, list): raise ValueError('invalid_wayback_document')
    if not data: return [], None
    header = data[0]
    if not isinstance(header, list) or not all(isinstance(field, str) for field in header) or 'original' not in header or len(set(header)) != len(header):
        raise ValueError('invalid_wayback_fields')
    entries, resume, separated = [], None, False
    for row in data[1:]:
        if row == []:
            if separated: raise ValueError('invalid_wayback_resume')
            separated = True
            continue
        if separated:
            if resume is not None or not isinstance(row, list) or len(row) != 1 or not isinstance(row[0], str) or not 1 <= len(row[0]) <= 8192:
                raise ValueError('invalid_wayback_resume')
            resume = row[0]
            continue
        if not isinstance(row, list) or len(row) != len(header) or not all(isinstance(x, str) for x in row):
            raise ValueError('invalid_wayback_record')
        value = dict(zip(header, row))
        if value.get('statuscode', '200') != '200': continue
        entries.append({'url': value['original'], 'archive_timestamp': value.get('timestamp'),
                        'archive_mime': value.get('mimetype'), 'archive_status': value.get('statuscode'),
                        'archive_digest': value.get('digest')})
    if separated and resume is None: raise ValueError('invalid_wayback_resume')
    return entries, resume


def crt_endpoint(host):
    return 'https://crt.sh/?' + urlencode({'q': '%.' + host, 'output': 'json'})


def parse_crt(document, base_host):
    data = json.loads(document)
    if not isinstance(data, list): raise ValueError('invalid_ct_document')
    names = {}
    for entry in data:
        if not isinstance(entry, dict): raise ValueError('invalid_ct_record')
        for field in ('common_name', 'name_value'):
            value = entry.get(field, '')
            if not isinstance(value, str): raise ValueError('invalid_ct_name')
            for raw in value.split('\n'):
                name = raw.strip().lower().removeprefix('*.').rstrip('.')
                if not name or len(name) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', name): continue
                if any(not part or len(part)>63 or part.startswith('-') or part.endswith('-') for part in name.split('.')): continue
                if name == base_host or name.endswith('.' + base_host):
                    names.setdefault(name, {'host': name, 'certificate_id': entry.get('id'),
                                           'certificate_not_before': entry.get('not_before'),
                                           'certificate_not_after': entry.get('not_after'),
                                           'wildcard_observed': raw.strip().startswith('*.')})
    return list(names.values())
