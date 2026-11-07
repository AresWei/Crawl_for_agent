"""Task-owned adaptation of active Crawl4AI seeder caches (838869b, Apache-2.0).

Retains TTL/force and sitemap lastmod decisions, replacing independent source
clients/files with scoped identities, bounded response snapshots and provenance.
No DomainMapper scan cache is implemented; its source helpers have no callsite.
"""
from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import time
import uuid
from urllib.parse import urlsplit

from ..core.accounts import AccountStore, ProxyStore
from ..core.records import canonical_json
from ..core.exclusive import lease
from .archives import SERVICE_HOSTS
from .parsers import decode_xml


def _hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical_json(value).encode()).hexdigest()


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def sitemap_lastmod(content, maximum):
    _, root = decode_xml(content, maximum)
    values = [value.strip() for value in root.xpath("//*[local-name()='lastmod']/text()") if value.strip()]
    # Frozen source compares the maximum lexical lastmod string, not HTTP Date.
    return max(values) if values else None


class DiscoveryCache:
    KINDS = {'sitemap', 'cc', 'cc_pages', 'cc_catalog', 'validate'}

    def __init__(self, context, *, clock=None):
        self.context = context
        self.clock = clock or time.time
        params = context.params
        self.enabled = params.get('discovery_cache', False)
        self.force = params.get('force', False)
        self.validate_lastmod = params.get('validate_sitemap_lastmod', True)
        if any(type(value) is not bool for value in (self.enabled, self.force, self.validate_lastmod)):
            raise ValueError('discovery_cache, force and validate_sitemap_lastmod must be booleans')
        self.ttl = params.get('cache_ttl_hours', 24)
        if type(self.ttl) not in (int, float) or not math.isfinite(self.ttl) or not 0 <= self.ttl <= 8760:
            raise ValueError('cache_ttl_hours must be finite and within 0..8760; zero disables TTL')
        self.maximum = params.get('discovery_cache_max_bytes', 64_000_000)
        if type(self.maximum) is not int or not 1024 <= self.maximum <= 256_000_000:
            raise ValueError('discovery_cache_max_bytes must be 1024..256000000')
        self.directory = context.paths.root / 'cache' / 'discovery'
        self.hits = self.misses = self.validations = self.write_skips = 0

    def key(self, item, config):
        params = self.context.params
        external = item['kind'] in SERVICE_HOSTS
        account = None if external else params.get('account')
        state = AccountStore(self.context.paths).state('web', account)
        proxy = ProxyStore(self.context.paths).get(params.get('proxy_id'))
        platform = 'web_discovery' if external else 'web'
        cookies = []
        for client_key, client in getattr(self.context, '_http_clients', {}).items():
            if client_key[0] == platform and client_key[1] == account and client_key[4] == params.get('proxy_id'):
                cookies.extend((cookie.domain, cookie.path, cookie.name, cookie.value, cookie.expires, cookie.secure)
                               for cookie in client.cookies.jar)
        # Private values participate only in the digest, never the cache receipt
        # or serialized key. Rotation of an alias's contents changes the digest.
        return _hash({'version': 2, 'kind': item['kind'], 'url': item['url'],
            'extract_head': bool(params.get('extract_head')) if item['kind'] == 'validate' else None,
            'account': account, 'state': state, 'proxy_id': params.get('proxy_id'), 'proxy': proxy,
            'session_cookies': sorted(cookies, key=lambda cookie: canonical_json(cookie)),
            'headers': {'User-Agent': 'ResearchCrawler/0.1 (local research)'},
            'selected_hosts': sorted(config['hosts']), 'selected_origins': sorted(config['origins'])})

    @staticmethod
    def _allowed(item, url, config):
        # Reuse the driver's current redirect policy, including the primary host
        # and exact HTTPS archive service. A cache hit cannot broaden that scope.
        from .runtime import in_scope
        service = SERVICE_HOSTS.get(item['kind'])
        try:
            return (urlsplit(url).hostname == service and urlsplit(url).scheme == 'https') if service else in_scope(url, config)
        except (TypeError, ValueError):
            return False

    def _read(self, key, maximum):
        path = self.directory / (key + '.json')
        try:
            if path.stat().st_size > min(self.maximum, maximum * 2 + 16384):
                return None, 'entry_byte_limit'
            entry = json.loads(path.read_text(encoding='utf-8'))
            if entry['version'] != 1 or entry['key'] != key:
                return None, 'invalid_entry'
            if (not all(isinstance(entry[name], str) for name in ('source_job_id', 'source_fetched_at', 'source_sha256'))
                    or not isinstance(entry['response'], dict)
                    or not isinstance(entry['response']['url'], str)
                    or type(entry['response']['status']) is not int):
                return None, 'invalid_entry'
            if self.ttl > 0 and self.clock() - entry['stored_at'] > self.ttl * 3600:
                return None, 'expired'
            content = base64.b64decode(entry['response'].pop('content_base64'), validate=True)
            if len(content) > maximum or _hash(content) != entry['source_sha256']:
                return None, 'invalid_content'
            entry['response']['content'] = content
            return entry, 'hit'
        except (OSError, ValueError, KeyError, TypeError):
            return None, 'miss'

    def _fresh(self, response, status, key=None):
        return {**response, 'source_fetched_at': _stamp(self.clock()),
            '_discovery_cache_key': key,
            'cache': {'status': status, 'source_job_id': self.context.job_id,
                      'source_sha256': _hash(response['content']), 'network_acquired_this_job': True}}

    async def acquire(self, item, config, fetcher):
        await self.context.check_control()
        if not self.enabled or item['kind'] not in self.KINDS:
            return self._fresh(await fetcher(self.context, item, config), 'disabled')
        key = self.key(item, config)
        entry, status = (None, 'forced') if self.force else await asyncio.to_thread(self._read, key, config['max_document_bytes'])
        if entry is not None and not self._allowed(item, entry['response']['url'], config):
            entry, status = None, 'scope_mismatch'
        await self.context.check_control()
        if entry is None:
            self.misses += 1
            return self._fresh(await fetcher(self.context, item, config), status, key)
        validation = None
        if item['kind'] == 'sitemap' and self.validate_lastmod:
            # The active upstream path fetches sitemap XML for lastmod before
            # deciding cache validity. HTTP HEAD alone cannot supply XML lastmod.
            fresh = self._fresh(await fetcher(self.context, item, config), 'lastmod_changed', key)
            self.validations += 1
            if not self._allowed(item, fresh['url'], config):
                raise ValueError('cache_validation_outside_selected_hosts')
            if not 200 <= fresh['status'] < 300:
                return fresh
            current = sitemap_lastmod(fresh['content'], config['max_document_bytes'] * 2)
            old = entry.get('sitemap_lastmod')
            if current and (not old or current > old):
                self.misses += 1
                return fresh
            validation_path = self.context.paths.run(self.context.job_id) / ('cache-validation-' + _hash(fresh['content']) + '.bin')
            validation_path.write_bytes(fresh['content'])
            validation = {'method': fresh.get('method', 'GET'), 'url': fresh['url'],
                'observed_at': fresh['source_fetched_at'], 'sha256': _hash(fresh['content']),
                'bytes': len(fresh['content']), 'artifact': str(validation_path),
                'current_lastmod': current, 'cached_lastmod': old,
                'outcome': 'lastmod_not_newer' if current and old else 'lastmod_unavailable',
                'body_equality_verified': fresh['content'] == entry['response']['content']}
        self.hits += 1
        receipt = {'status': 'hit', 'source_job_id': entry['source_job_id'],
            'source_sha256': entry['source_sha256'], 'stored_at': entry['stored_at'],
            'reused_at': _stamp(self.clock()), 'network_acquired_this_job': False,
            'validation': validation, 'freshness_scope': 'xml_lastmod_signal' if validation and validation['outcome'] == 'lastmod_not_newer' else 'ttl_only'}
        return {**entry['response'], 'source_fetched_at': entry['source_fetched_at'], 'cache': receipt}

    async def save(self, item, response, config):
        """Call only after successful parsing; do not mutate evidence receipts."""
        if (not self.enabled or item['kind'] not in self.KINDS or
                response['cache']['status'] == 'hit' or not 200 <= response['status'] < 300):
            return
        key = self.key(item, config)
        if not self._allowed(item, response['url'], config):
            self.write_skips += 1
            return
        if key != response.get('_discovery_cache_key'):
            self.write_skips += 1
            return
        content = response['content']
        if len(content) > config['max_document_bytes']:
            return
        # No response headers are retained: in particular no Set-Cookie/auth.
        safe = {name: response[name] for name in ('url', 'status', 'encoding', 'method') if name in response}
        safe['content_base64'] = base64.b64encode(content).decode('ascii')
        entry = {'version': 1, 'key': key, 'stored_at': self.clock(),
            'source_job_id': self.context.job_id, 'source_fetched_at': response['source_fetched_at'],
            'source_sha256': _hash(content), 'response': safe,
            'sitemap_lastmod': sitemap_lastmod(content, config['max_document_bytes'] * 2) if item['kind'] == 'sitemap' else None}
        data = canonical_json(entry).encode()
        # Reuse the core OS lease, releasing it after this one cache write rather
        # than holding a second resource owner for the whole job.
        async with AsyncExitStack() as resources:
            scoped = copy.copy(self.context)
            scoped.resources = resources
            await lease(scoped, 'discovery-cache', 'write')
            self._write(key, data)

    def _write(self, key, data):
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            paths = []
            for path in self.directory.glob('*.json'):
                paths.append(path)
                if len(paths) > 2048:
                    self.write_skips += 1
                    return
            target = self.directory / (key + '.json')
            size = sum(path.stat().st_size for path in paths if path != target)
            if len(paths) >= 2048 and not target.exists() or size + len(data) > self.maximum:
                self.write_skips += 1
                return
            temporary = self.directory / (key + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
        except OSError:
            self.write_skips += 1
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.write_skips += 1

    def evidence(self):
        return {'enabled': self.enabled, 'hits': self.hits, 'misses': self.misses,
                'sitemap_validations': self.validations, 'write_skips': self.write_skips}
