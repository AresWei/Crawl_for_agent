"""Reusable page cache, partitioned by identity and capture options, separate from evidence."""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from ..core.accounts import AccountStore
from .cache_context import CacheContext, CacheMode


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class PageCache:
    def __init__(self, ctx):
        self.ctx = ctx
        self.mode = CacheMode(ctx.params.get("cache_mode", "bypass"))
        if any(ctx.params.get(key) for key in ('cdp_url','c4a_script','browser_profile','hooks','js_code','js_code_before_wait')):
            self.mode = CacheMode.BYPASS
        self.ttl = max(0, float(ctx.params.get("cache_ttl_seconds", 3600)))
        ignored = {"urls", "max_pages", "max_depth", "max_requests", "max_network_bytes", "max_seconds",
                   "cache_mode", "cache_ttl_seconds", "cache_validate", "strategy", "url_filters", "url_scorer", "concurrency"}
        options = {k: v for k, v in ctx.params.items() if k not in ignored}
        state = AccountStore(ctx.paths).state("web", ctx.params.get("account"))
        self.partition = hashlib.sha256(json.dumps([options, state], sort_keys=True, default=str).encode()).hexdigest()
        self.directory = ctx.paths.root / "cache" / "web-pages"

    def path(self, url):
        key = hashlib.sha256((self.partition + url).encode()).hexdigest()
        return self.directory / (key + ".json")

    def read(self, url):
        if not CacheContext(url, self.mode).should_read():
            return None, "bypassed"
        path = self.path(url)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None, "miss"
        if time.time() - entry["stored_at"] > self.ttl and not self.ctx.params.get("cache_validate"):
            return None, "expired"
        # Local files must never be reused after the input itself changes.
        if entry.get("local_fingerprint") and entry["local_fingerprint"] != self.local_fingerprint(url):
            return None, "source_changed"
        payload = entry["payload"]
        payload["cache"] = {"status": "hit", "source_fetched_at": payload["source_fetched_at"],
                            "source_job_id": entry["source_job_id"], "reused_at": utc_now()}
        return payload, "hit"

    @staticmethod
    def local_fingerprint(url):
        if not url.startswith("file:"):
            return None
        from .inputs import local_path
        path = local_path(url)
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def write(self, url, payload):
        if not CacheContext(url, self.mode).should_write() or payload.get("artifacts"):
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {"stored_at": time.time(), "source_job_id": self.ctx.job_id, "payload": payload,
                 "local_fingerprint": self.local_fingerprint(url)}
        target = self.path(url)
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
