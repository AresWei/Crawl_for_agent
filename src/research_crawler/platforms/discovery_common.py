"""Independent discovery helpers using the existing task HTTP and checkpoint core."""
from __future__ import annotations

import hashlib
import json

from ..core.jobs import NeedsLogin
from ..core.records import utcnow
from .weibo._shared import Client


class DiscoveryUnavailable(Exception):
    def __init__(self, reason, evidence):
        self.reason, self.evidence = reason, evidence
        super().__init__(reason)


class DiscoveryClient(Client):
    """No retry, browser escalation or alternate-host fallback on access denial."""

    async def get_json(self, url, params):
        await self.throttle()
        response = await self.http.get(url, params=params)
        evidence = {"url": str(response.request.url), "observed_at": utcnow(),
                    "http_status": response.status_code, "bytes": len(response.content),
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                    "content_type": response.headers.get("content-type", "")}
        self.last_evidence = evidence
        if response.status_code == 401:
            raise NeedsLogin(self.platform, self.account)
        if response.status_code in (403, 412, 429):
            raise DiscoveryUnavailable("rate_limited" if response.status_code == 429 else "blocked", evidence)
        if response.status_code != 200:
            raise DiscoveryUnavailable("http_error", evidence)
        try:
            return response.json(), evidence
        except (ValueError, UnicodeError):
            raise DiscoveryUnavailable("non_json_response", evidence) from None


def schema_error(evidence, reason="invalid_source_schema"):
    return DiscoveryUnavailable(reason, evidence)


async def unavailable_summary(context, platform, mode, error):
    # Keep the last failed request as an atomic evidence checkpoint; successful
    # page checkpoints remain untouched so retry resumes the precise stream.
    stream = f"discovery:{platform}:access"
    cp = await context.checkpoint(stream)
    summary = {"status": "partial", "platform": platform, "mode": mode,
               "reason": error.reason, "access_state": error.reason,
               "coverage_complete": False, "evidence": error.evidence}
    await context.commit_page(stream, [], next_cursor=None, exhausted=False,
        expected_revision=cp["revision"] if cp else 0, metadata={"summary": summary})
    return summary


def scope_key(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]
