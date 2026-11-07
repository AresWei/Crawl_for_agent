# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
from functools import lru_cache
from urllib.parse import urlparse, urlunparse


def normalize_url_for_deep_crawl(href, base_url, preserve_https=False, original_scheme=None):
    """Normalize URLs to ensure consistent format"""
    from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

    # Handle None or empty values
    if not href:
        return None

    # Use urljoin to handle relative URLs
    full_url = urljoin(base_url, href.strip())
    
    # Preserve HTTPS if requested and original scheme was HTTPS
    if preserve_https and original_scheme == 'https':
        parsed_full = urlparse(full_url)
        parsed_base = urlparse(base_url)
        # Only preserve HTTPS for same-domain links (not protocol-relative URLs)
        # Protocol-relative URLs (//example.com) should follow the base URL's scheme
        if (parsed_full.scheme == 'http' and 
            parsed_full.netloc == parsed_base.netloc and
            not href.strip().startswith('//')):
            full_url = full_url.replace('http://', 'https://', 1)
    
    # Parse the URL for normalization
    parsed = urlparse(full_url)
    
    # Convert hostname to lowercase
    netloc = parsed.netloc.lower()
    
    # Remove fragment entirely
    fragment = ''
    
    # Normalize query parameters if needed
    query = parsed.query
    if query:
        # Parse query parameters
        params = parse_qs(query)
        
        # Remove tracking parameters (example - customize as needed)
        tracking_params = ['utm_source', 'utm_medium', 'utm_campaign', 'ref', 'fbclid']
        for param in tracking_params:
            if param in params:
                del params[param]
                
        # Rebuild query string, sorted for consistency
        query = urlencode(params, doseq=True) if params else ''
    
    # Build normalized URL
    normalized = urlunparse((
        parsed.scheme,
        netloc,
        parsed.path or '/',  # Preserve trailing slash
        parsed.params,
        query,
        fragment
    ))
    
    return normalized


@lru_cache(maxsize=10000)
def efficient_normalize_url_for_deep_crawl(href, base_url, preserve_https=False, original_scheme=None):
    """Efficient URL normalization with proper parsing"""
    from urllib.parse import urljoin
    
    if not href:
        return None
    
    # Resolve relative URLs
    full_url = urljoin(base_url, href.strip())
    
    # Preserve HTTPS if requested and original scheme was HTTPS
    if preserve_https and original_scheme == 'https':
        parsed_full = urlparse(full_url)
        parsed_base = urlparse(base_url)
        # Only preserve HTTPS for same-domain links (not protocol-relative URLs)
        # Protocol-relative URLs (//example.com) should follow the base URL's scheme
        if (parsed_full.scheme == 'http' and 
            parsed_full.netloc == parsed_base.netloc and
            not href.strip().startswith('//')):
            full_url = full_url.replace('http://', 'https://', 1)
    
    # Use proper URL parsing
    parsed = urlparse(full_url)
    
    # Only perform the most critical normalizations
    # 1. Lowercase hostname
    # 2. Remove fragment
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        parsed.path or '/',  # Preserve trailing slash
        parsed.params,
        parsed.query,
        ''  # Remove fragment
    ))
    
    return normalized
