"""Source-derived dispatch policies, using the unified job's existing resources.

Adapted from Crawl4AI async_dispatcher.py (RateLimiter, BaseDispatcher,
MemoryAdaptiveDispatcher, SemaphoreDispatcher) and CrawlerRunConfig.is_match,
commit 838869bdda93f25e6d50b3f50ae9577d9b19a586. Upstream Apache-2.0
license and attribution remain in licenses/ and upstream/. See dispatch-source-manifest.json.

The durable frontier owns pending/inflight items and commits; this module never
removes or requeues them. It owns no HTTP client, browser, background task or store.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from fnmatch import fnmatchcase
import math
import random
import time
from urllib.parse import urlsplit

import httpx
import psutil


class DispatchMemoryPressure(ValueError):
    """Admission paused beyond its bounded memory wait; leave frontier pending."""


def _number(value, minimum, maximum, name):
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"dispatch {name} must be between {minimum} and {maximum}")
    return value


def _options(spec):
    spec = dict(spec)
    allowed = {'base_delay', 'max_delay', 'max_retries', 'retry_statuses'}
    if set(spec) - allowed:
        raise ValueError('Unknown per-URL dispatch options')
    delay = spec.get('base_delay', [0, 0])
    if not isinstance(delay, (tuple, list)) or len(delay) != 2:
        raise ValueError('dispatch base_delay requires [minimum, maximum]')
    low, high = [_number(v, 0, 60, 'base_delay') for v in delay]
    maximum = _number(spec.get('max_delay', 60), 0, 120, 'max_delay')
    if low > high or high > maximum:
        raise ValueError('dispatch delays must satisfy minimum <= maximum <= max_delay')
    retries = _number(spec.get('max_retries', 0), 0, 5, 'max_retries')
    if int(retries) != retries:
        raise ValueError('dispatch max_retries must be an integer')
    statuses = spec.get('retry_statuses', [429, 502, 504])
    if not isinstance(statuses, list) or any(code not in {429, 502, 504} for code in statuses):
        raise ValueError('dispatch retry_statuses supports only 429, 502 and 504')
    return {'base_delay': (low, high), 'max_delay': maximum,
            'max_retries': int(retries), 'retry_statuses': statuses}


def retry_after_seconds(value, *, now=None):
    """RFC HTTP-date or nonnegative delta; invalid values do not shorten backoff."""
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = (parsed - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


@dataclass
class DomainState:
    next_allowed: float = 0
    current_delay: float = 0
    fail_count: int = 0


class DispatchPolicy:
    def __init__(self, params, *, concurrency=None, context=None, memory_reader=None):
        config = params.get('dispatch') or {}
        if not isinstance(config, dict):
            raise ValueError('dispatch must be an object')
        allowed = {'mode', 'memory_threshold_percent', 'critical_threshold_percent',
                   'recovery_threshold_percent', 'memory_wait_timeout', 'check_interval',
                   'base_delay', 'max_delay', 'max_retries', 'retry_statuses', 'url_configs'}
        if set(config) - allowed:
            raise ValueError('Unknown dispatch option')
        self.mode = config.get('mode', 'semaphore')
        if self.mode not in {'semaphore', 'memory_adaptive'}:
            raise ValueError('dispatch mode must be semaphore or memory_adaptive')
        self.concurrency = max(1, min(8, int(concurrency or params.get('concurrency', 2))))
        self.context = context
        self._memory_reader = memory_reader or (lambda: psutil.virtual_memory().percent)
        self.threshold = _number(config.get('memory_threshold_percent', 90), 1, 100, 'memory_threshold_percent')
        self.critical = _number(config.get('critical_threshold_percent', 95), 1, 100, 'critical_threshold_percent')
        self.recovery = _number(config.get('recovery_threshold_percent', 85), 0, 100, 'recovery_threshold_percent')
        if not self.recovery < self.threshold <= self.critical:
            raise ValueError('dispatch memory thresholds require recovery < threshold <= critical')
        self.check_interval = _number(config.get('check_interval', .25), .01, 5, 'check_interval')
        self.memory_timeout = _number(config.get('memory_wait_timeout', 10), 0, 120, 'memory_wait_timeout')
        self.defaults = _options({k: config[k] for k in ('base_delay','max_delay','max_retries','retry_statuses') if k in config})
        self.configs = config.get('url_configs')
        if self.configs is not None:
            if not isinstance(self.configs, list) or len(self.configs) > 100:
                raise ValueError('dispatch url_configs requires at most 100 entries')
            for entry in self.configs:
                if not isinstance(entry, dict) or set(entry) - {'pattern','options','match_mode'}:
                    raise ValueError('Invalid dispatch URL configuration')
                patterns = entry.get('pattern', '*')
                patterns = [patterns] if isinstance(patterns, str) else patterns
                if not isinstance(patterns, list) or not all(isinstance(p, str) and len(p) <= 2048 for p in patterns):
                    raise ValueError('dispatch pattern must be a glob string or list')
                if entry.get('match_mode', 'or') not in {'or','and'}:
                    raise ValueError('dispatch match_mode must be or or and')
                _options({**self.defaults, **entry.get('options', {})})
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._hosts, self._domains = {}, {}
        self._pressure = False
        self._stats = {'started': 0, 'succeeded': 0, 'failed': 0, 'cancelled': 0,
                       'retries': 0, 'active': 0, 'peak_active': 0, 'wait_seconds': 0.,
                       'memory_wait_seconds': 0., 'memory_pressure_events': 0,
                       'memory_percent': None, 'peak_memory_percent': None,
                       'peak_sampled_process_rss_bytes': 0, 'retry_after_exceeds_limit': 0,
                       'no_config_match': 0}

    def select_config(self, url):
        # Upstream first-match semantics; only data-only dispatch overrides here.
        if self.configs is None:
            return self.defaults
        for config in self.configs:
            patterns = config.get('pattern', '*')
            patterns = [patterns] if isinstance(patterns, str) else patterns
            matches = [fnmatchcase(url, pattern) for pattern in patterns]
            if matches and (all(matches) if config.get('match_mode', 'or') == 'and' else any(matches)):
                return _options({**self.defaults, **config.get('options', {})})
        return None

    async def _control(self):
        if self.context is not None:
            await self.context.check_control()

    async def _sleep(self, duration, counter='wait_seconds'):
        start, end = time.monotonic(), time.monotonic() + duration
        try:
            while time.monotonic() < end:
                await self._control()
                await asyncio.sleep(min(.1, max(0, end - time.monotonic())))
        finally:
            self._stats[counter] += time.monotonic() - start

    async def capacity(self, *, wait=True):
        await self._control()
        if self.mode == 'semaphore':
            return self.concurrency
        started = time.monotonic()
        while True:
            percent = _number(self._memory_reader(), 0, 100, 'measured memory')
            self._stats['memory_percent'] = percent
            self._stats['peak_memory_percent'] = max(self._stats['peak_memory_percent'] or 0, percent)
            if percent >= self.threshold and not self._pressure:
                self._pressure = True
                self._stats['memory_pressure_events'] += 1
            elif self._pressure and percent <= self.recovery:
                self._pressure = False
            if not self._pressure:
                return self.concurrency
            if not wait:
                return 0
            if time.monotonic() - started >= self.memory_timeout:
                raise DispatchMemoryPressure('memory_pressure')
            await self._sleep(min(self.check_interval, self.memory_timeout - (time.monotonic() - started)), 'memory_wait_seconds')

    def _retry_delay(self, error, options, state, attempt):
        retry_after = None
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            headers = response.headers
            retry_after = retry_after_seconds(headers.get('retry-after'))
            # Even a non-replayable 503/challenge may carry a server embargo.
            if retry_after is not None:
                state.next_allowed = max(state.next_allowed, time.monotonic() + retry_after)
            if (response.status_code not in options['retry_statuses'] or
                    headers.get('cf-mitigated') == 'challenge' or headers.get('www-authenticate') or
                    any(marker in headers.get('location', '').lower() for marker in ('login','signin','captcha','challenge'))):
                return None
        elif not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError)):
            return None
        if retry_after is not None:
            state.next_allowed = max(state.next_allowed, time.monotonic() + retry_after)
        if retry_after is not None and retry_after > options['max_delay']:
            self._stats['retry_after_exceeds_limit'] += 1
            # Preserve the server's embargo for subsequent URLs too; never cap it down.
            state.next_allowed = max(state.next_allowed, time.monotonic() + retry_after)
            return None
        state.fail_count += 1
        # Original exponential backoff and jitter, with a nonzero seed when delay=0.
        state.current_delay = min(max(.1, state.current_delay) * 2 * random.uniform(.75, 1.25), options['max_delay'])
        delay = max(state.current_delay, retry_after or 0)
        state.next_allowed = max(state.next_allowed, time.monotonic() + delay)
        return delay if attempt < options['max_retries'] else None

    async def run(self, item_or_url, operation, *, method='GET', retry_allowed=False):
        url = item_or_url if isinstance(item_or_url, str) else item_or_url['url']
        options = self.select_config(url)
        if options is None:
            self._stats['no_config_match'] += 1
            raise ValueError('no_dispatch_config_match')
        host = urlsplit(url).hostname or urlsplit(url).scheme
        lock = self._hosts.setdefault(host, asyncio.Lock())
        state = self._domains.setdefault(host, DomainState())
        # Acquire host first: a queued same-host request must not occupy a global slot.
        async with lock:
            for attempt in range(options['max_retries'] + 1):
                await self._control()
                await self._sleep(max(0, state.next_allowed - time.monotonic()))
                async with self._semaphore:
                    await self.capacity()
                    self._stats['started'] += 1
                    self._stats['active'] += 1
                    self._stats['peak_active'] = max(self._stats['peak_active'], self._stats['active'])
                    self._stats['peak_sampled_process_rss_bytes'] = max(self._stats['peak_sampled_process_rss_bytes'], psutil.Process().memory_info().rss)
                    state.current_delay = max(state.current_delay, random.uniform(*options['base_delay']))
                    state.next_allowed = time.monotonic() + state.current_delay
                    try:
                        result = await operation()
                    except asyncio.CancelledError:
                        self._stats['cancelled'] += 1
                        raise
                    except Exception as error:
                        # Observe rate signals even when this operation cannot be replayed.
                        delay = self._retry_delay(error, options, state, attempt)
                        if not retry_allowed or method.upper() not in {'GET','HEAD'}:
                            delay = None
                        if delay is None:
                            self._stats['failed'] += 1
                            raise
                        state.next_allowed = max(state.next_allowed, time.monotonic() + delay)
                        self._stats['retries'] += 1
                    else:
                        self._stats['succeeded'] += 1
                        state.current_delay = max(random.uniform(*options['base_delay']), state.current_delay * .75)
                        state.fail_count = 0
                        return result
                    finally:
                        self._stats['active'] -= 1

    def stats(self):
        return {**self._stats, 'mode': self.mode, 'concurrency': self.concurrency,
                'per_host_concurrency': 1, 'memory_state': 'critical' if (self._stats['memory_percent'] or 0) >= self.critical else 'pressure' if self._pressure else 'normal',
                'memory_measurement': 'system_psutil_percent', 'rss_measurement': 'process_sample_at_request_start',
                'host_states': len(self._domains)}

    def checkpoint_state(self):
        """Persist only timing state; wall time survives a process restart."""
        monotonic, wall = time.monotonic(), time.time()
        return {'version': 1, 'hosts': {host: {
            'not_before_unix': wall + max(0, state.next_allowed - monotonic),
            'current_delay': state.current_delay, 'fail_count': state.fail_count}
            for host, state in self._domains.items()}}

    def restore_state(self, snapshot):
        if not snapshot:
            return
        if snapshot.get('version') != 1 or not isinstance(snapshot.get('hosts'), dict) or len(snapshot['hosts']) > 10000:
            raise ValueError('Invalid dispatch checkpoint')
        monotonic, wall = time.monotonic(), time.time()
        for host, state in snapshot['hosts'].items():
            until = float(state['not_before_unix'])
            if not math.isfinite(until):
                raise ValueError('Invalid dispatch checkpoint time')
            self._domains[host] = DomainState(monotonic + max(0, until - wall),
                _number(state['current_delay'], 0, 120, 'checkpoint delay'),
                int(_number(state['fail_count'], 0, 100000, 'checkpoint failures')))
