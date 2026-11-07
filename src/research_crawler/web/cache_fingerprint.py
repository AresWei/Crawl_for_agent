# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
import re, hashlib


def compute_head_fingerprint(head_html: str) -> str:
    """
    Compute a fingerprint of <head> content for cache validation.

    Focuses on content that typically changes when page updates:
    - <title>
    - <meta name="description">
    - <meta property="og:title|og:description|og:image|og:updated_time">
    - <meta property="article:modified_time">
    - <meta name="last-modified">

    Uses xxhash for speed, combines multiple signals into a single hash.

    Args:
        head_html: The HTML content of the <head> section

    Returns:
        A hex string fingerprint, or empty string if no signals found
    """
    if not head_html:
        return ""

    head_lower = head_html.lower()
    signals = []

    # Extract title
    title_match = re.search(r'<title[^>]*>(.*?)</title>', head_lower, re.DOTALL)
    if title_match:
        signals.append(title_match.group(1).strip())

    # Meta tags to extract (name or property attribute, and the value to match)
    meta_tags = [
        ("name", "description"),
        ("name", "last-modified"),
        ("property", "og:title"),
        ("property", "og:description"),
        ("property", "og:image"),
        ("property", "og:updated_time"),
        ("property", "article:modified_time"),
    ]

    for attr_type, attr_value in meta_tags:
        # Handle both attribute orders: attr="value" content="..." and content="..." attr="value"
        patterns = [
            rf'<meta[^>]*{attr_type}=["\']{ re.escape(attr_value)}["\'][^>]*content=["\']([^"\']*)["\']',
            rf'<meta[^>]*content=["\']([^"\']*)["\'][^>]*{attr_type}=["\']{re.escape(attr_value)}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, head_lower)
            if match:
                signals.append(match.group(1).strip())
                break  # Found this tag, move to next

    if not signals:
        return ""

    # Combine signals and hash
    combined = '|'.join(signals)
    return hashlib.sha256(combined.encode()).hexdigest()
