"""Evidence-only integration of the pinned Crawl4AI blocking heuristics.

The detector does not fetch, retry, solve challenges, select proxies or invoke
any provider. The caller retains source and owns coverage/answer eligibility.
"""
from __future__ import annotations

from .antibot_detector import is_blocked


def assess_blocking(status_code, html, *, enabled=False, source_kind="http",
                    downloaded=False, placeholder=False):
    """Return a bounded decision receipt; positives are suspected, not proven.

    Opt-in is deliberate: the upstream algorithm also marks some legitimate
    small pages and security-related content. A negative is not proof of access
    or completeness. Raw/local/binary content cannot establish a remote block.
    """
    if type(enabled) is not bool:
        raise ValueError("detect_blocking must be a boolean")
    if status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
        raise ValueError("Invalid HTTP status for block detection")
    if not isinstance(html, str):
        raise ValueError("Block detection expects decoded text")
    receipt = {
        "enabled": enabled,
        "status": "disabled",
        "blocked": None,
        "reason": "",
        "exclude_from_answers": False,
        "heuristic": True,
        "detector": "crawl4ai-838869b-antibot",
        "source_kind": source_kind,
        "http_status": status_code,
        "limitations": [
            "A match can be a legitimate small page or content discussing security.",
            "No match does not prove access, completeness or absence of challenges.",
        ],
    }
    if not enabled:
        return receipt
    skip = ("caller_supplied_source" if source_kind in {"raw", "local", "file"}
            else "binary_source" if source_kind in {"pdf", "binary", "download"} or downloaded
            else "placeholder_source" if placeholder
            else "unsupported_source_kind" if source_kind not in {"http", "browser"}
            else None)
    if skip:
        receipt.update(status="skipped", reason=skip)
        return receipt
    blocked, reason = is_blocked(status_code, html)
    receipt.update(status="suspected_block" if blocked else "clear", blocked=blocked,
                   reason=reason, exclude_from_answers=blocked)
    return receipt
