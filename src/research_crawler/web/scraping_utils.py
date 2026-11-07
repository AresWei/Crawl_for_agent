# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
import re
import lxml.html
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode


def extract_metadata_using_lxml(html, doc=None):
    """
    Extract metadata from HTML using lxml for better performance.
    """
    metadata = {}

    if not html and doc is None:
        return {}

    if doc is None:
        try:
            doc = lxml.html.document_fromstring(html)
        except Exception:
            return {}

    # Use XPath to find head element
    head = doc.xpath("//head")
    if not head:
        return metadata

    head = head[0]

    # Title - using XPath
    # title = head.xpath(".//title/text()")
    # metadata["title"] = title[0].strip() if title else None

    # === Title Extraction - New Approach ===
    # Attempt to extract <title> using XPath
    title = head.xpath(".//title/text()")
    title = title[0] if title else None

    # Fallback: Use .find() in case XPath fails due to malformed HTML
    if not title:
        title_el = doc.find(".//title")
        title = title_el.text if title_el is not None else None

    # Final fallback: Use OpenGraph or Twitter title if <title> is missing or empty
    if not title:
        title_candidates = (
            doc.xpath("//meta[@property='og:title']/@content") or
            doc.xpath("//meta[@name='twitter:title']/@content")
        )
        title = title_candidates[0] if title_candidates else None

    # Strip and assign title
    metadata["title"] = title.strip() if title else None

    # Meta description - using XPath with multiple attribute conditions
    description = head.xpath('.//meta[@name="description"]/@content')
    metadata["description"] = description[0].strip() if description else None

    # Meta keywords
    keywords = head.xpath('.//meta[@name="keywords"]/@content')
    metadata["keywords"] = keywords[0].strip() if keywords else None

    # Meta author
    author = head.xpath('.//meta[@name="author"]/@content')
    metadata["author"] = author[0].strip() if author else None

    # Open Graph metadata - using starts-with() for performance
    og_tags = head.xpath('.//meta[starts-with(@property, "og:")]')
    for tag in og_tags:
        property_name = tag.get("property", "").strip()
        content = tag.get("content", "").strip()
        if property_name and content:
            metadata[property_name] = content

    # Twitter Card metadata
    twitter_tags = head.xpath('.//meta[starts-with(@name, "twitter:")]')
    for tag in twitter_tags:
        property_name = tag.get("name", "").strip()
        content = tag.get("content", "").strip()
        if property_name and content:
            metadata[property_name] = content
   
   # Article metadata
    article_tags = head.xpath('.//meta[starts-with(@property, "article:")]')
    for tag in article_tags:
        property_name = tag.get("property", "").strip()
        content = tag.get("content", "").strip()
        if property_name and content:
            metadata[property_name] = content

    return metadata


def extract_metadata(html, soup=None):
    """
    Extract optimized content, media, and links from website HTML.

    How it works:
    1. Similar to `get_content_of_website`, but optimized for performance.
    2. Filters and scores images for usefulness.
    3. Extracts contextual descriptions for media files.
    4. Handles excluded tags and CSS selectors.
    5. Cleans HTML and converts it to Markdown.

    Args:
        url (str): The website URL.
        html (str): The HTML content of the website.
        word_count_threshold (int): Minimum word count for content inclusion. Defaults to MIN_WORD_THRESHOLD.
        css_selector (Optional[str]): CSS selector to extract specific content. Defaults to None.
        **kwargs: Additional options for customization.

    Returns:
        Dict[str, Any]: Extracted content including Markdown, cleaned HTML, media, links, and metadata.
    """

    metadata = {}

    if not html and not soup:
        return {}

    if not soup:
        soup = BeautifulSoup(html, "lxml")

    head = soup.head
    if not head:
        return metadata

    # Title
    title_tag = head.find("title")
    metadata["title"] = (
        title_tag.string.strip() if title_tag and title_tag.string else None
    )

    # Meta description
    description_tag = head.find("meta", attrs={"name": "description"})
    metadata["description"] = (
        description_tag.get("content", "").strip() if description_tag else None
    )

    # Meta keywords
    keywords_tag = head.find("meta", attrs={"name": "keywords"})
    metadata["keywords"] = (
        keywords_tag.get("content", "").strip() if keywords_tag else None
    )

    # Meta author
    author_tag = head.find("meta", attrs={"name": "author"})
    metadata["author"] = author_tag.get("content", "").strip() if author_tag else None

    # Open Graph metadata
    og_tags = head.find_all("meta", attrs={"property": re.compile(r"^og:")})
    for tag in og_tags:
        property_name = tag.get("property", "").strip()
        content = tag.get("content", "").strip()
        if property_name and content:
            metadata[property_name] = content

    # Twitter Card metadata
    twitter_tags = head.find_all("meta", attrs={"name": re.compile(r"^twitter:")})
    for tag in twitter_tags:
        property_name = tag.get("name", "").strip()
        content = tag.get("content", "").strip()
        if property_name and content:
            metadata[property_name] = content
    
    # Article metadata
    article_tags = head.find_all("meta", attrs={"property": re.compile(r"^article:")})
    for tag in article_tags:
        property_name = tag.get("property", "").strip()
        content = tag.get("content", "").strip()
        if property_name and content:
            metadata[property_name] = content
    
    return metadata


def normalize_url(
    href: str,
    base_url: str,
    *,
    drop_query_tracking=True,
    sort_query=True,
    keep_fragment=False,
    extra_drop_params=None,
    preserve_https=False,
    original_scheme=None
):
    """
    Extended URL normalizer

    Parameters
    ----------
    href : str
        The raw link extracted from a page.
    base_url : str
        The page’s canonical URL (used to resolve relative links).
    drop_query_tracking : bool (default True)
        Remove common tracking query parameters.
    sort_query : bool (default True)
        Alphabetically sort query keys for deterministic output.
    keep_fragment : bool (default False)
        Preserve the hash fragment (#section) if you need in-page links.
    extra_drop_params : Iterable[str] | None
        Additional query keys to strip (case-insensitive).

    Returns
    -------
    str | None
        A clean, canonical URL or None if href is empty/None.
    """
    if not href:
        return None

    # Resolve relative paths first
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

    # Parse once, edit parts, then rebuild
    parsed = urlparse(full_url)

    # ── netloc ──
    netloc = parsed.netloc.lower()

    # ── path ──
    # Strip duplicate slashes and trailing "/" (except root)
    # IMPORTANT: Don't use quote(unquote()) as it mangles + signs in URLs
    # The path from urlparse is already properly encoded
    path = parsed.path
    # Preserve trailing slashes -- they are semantically significant per RFC 3986
    # e.g. /page/9123/ and /page/9123 may return different responses

    # ── query ──
    query = parsed.query
    if query:
        # explode, mutate, then rebuild
        params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)]

        if drop_query_tracking:
            default_tracking = {
                'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
                'utm_content', 'gclid', 'fbclid', 'ref', 'ref_src'
            }
            if extra_drop_params:
                default_tracking |= {p.lower() for p in extra_drop_params}
            params = [(k, v) for k, v in params if k.lower() not in default_tracking]

        if sort_query:
            params.sort(key=lambda kv: kv[0])

        query = urlencode(params, doseq=True) if params else ''

    # ── fragment ──
    fragment = parsed.fragment if keep_fragment else ''

    # Re-assemble
    normalized = urlunparse((
        parsed.scheme,
        netloc,
        path,
        parsed.params,
        query,
        fragment
    ))

    return normalized


def get_base_domain(url: str) -> str:
    """
    Extract the base domain from a given URL, handling common edge cases.

    How it works:
    1. Parses the URL to extract the domain.
    2. Removes the port number and 'www' prefix.
    3. Handles special domains (e.g., 'co.uk') to extract the correct base.

    Args:
        url (str): The URL to extract the base domain from.

    Returns:
        str: The extracted base domain or an empty string if parsing fails.
    """
    try:
        # Get domain from URL
        domain = urlparse(url).netloc.lower()
        if not domain:
            return ""

        # Remove port if present
        domain = domain.split(":")[0]

        # Remove www
        domain = re.sub(r"^www\.", "", domain)

        # Extract last two parts of domain (handles co.uk etc)
        parts = domain.split(".")
        if len(parts) > 2 and parts[-2] in {
            "co",
            "com",
            "org",
            "gov",
            "edu",
            "net",
            "mil",
            "int",
            "ac",
            "ad",
            "ae",
            "af",
            "ag",
        }:
            return ".".join(parts[-3:])

        return ".".join(parts[-2:])
    except Exception:
        return ""


def is_external_url(url: str, base_domain: str) -> bool:
    """
    Extract the base domain from a given URL, handling common edge cases.

    How it works:
    1. Parses the URL to extract the domain.
    2. Removes the port number and 'www' prefix.
    3. Handles special domains (e.g., 'co.uk') to extract the correct base.

    Args:
        url (str): The URL to extract the base domain from.

    Returns:
        str: The extracted base domain or an empty string if parsing fails.
    """
    special = {"mailto:", "tel:", "ftp:", "file:", "data:", "javascript:"}
    if any(url.lower().startswith(p) for p in special):
        return True

    try:
        parsed = urlparse(url)
        if not parsed.netloc:  # Relative URL
            return False

        # Strip port and 'www.' from both domains for comparison
        url_domain = parsed.netloc.lower().split(":")[0].replace("www.", "")
        base = base_domain.lower().split(":")[0].replace("www.", "")

        # Check if URL domain ends with base domain
        return not url_domain.endswith(base)
    except Exception:
        return False


def extract_page_context(page_title: str, headlines_text: str, meta_description: str, base_url: str) -> dict:
    """
    Extract page context for link scoring - called ONCE per page for performance.
    Parser-agnostic function that takes pre-extracted data.
    
    Args:
        page_title: Title of the page
        headlines_text: Combined text from h1, h2, h3 elements
        meta_description: Meta description content
        base_url: Base URL of the page
        
    Returns:
        Dictionary containing page context data for fast link scoring
    """
    context = {
        'terms': set(),
        'headlines': headlines_text or '',
        'meta_description': meta_description or '',
        'domain': '',
        'is_docs_site': False
    }
    
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        context['domain'] = parsed.netloc.lower()
        
        # Check if this is a documentation/reference site
        context['is_docs_site'] = any(indicator in context['domain'] 
                                    for indicator in ['docs.', 'api.', 'developer.', 'reference.'])
        
        # Create term set for fast intersection (performance optimization)
        all_text = ((page_title or '') + ' ' + context['headlines'] + ' ' + context['meta_description']).lower()
        # Simple tokenization - fast and sufficient for scoring
        context['terms'] = set(word.strip('.,!?;:"()[]{}') 
                             for word in all_text.split() 
                             if len(word.strip('.,!?;:"()[]{}')) > 2)
                             
    except Exception:
        # Fail gracefully - return empty context
        pass
    
    return context


def calculate_link_intrinsic_score(
    link_text: str, 
    url: str, 
    title_attr: str, 
    class_attr: str, 
    rel_attr: str, 
    page_context: dict
) -> float:
    """
    Ultra-fast link quality scoring using only provided data (no DOM access needed).
    Parser-agnostic function.
    
    Args:
        link_text: Text content of the link
        url: Link URL
        title_attr: Title attribute of the link
        class_attr: Class attribute of the link
        rel_attr: Rel attribute of the link
        page_context: Pre-computed page context from extract_page_context()
        
    Returns:
        Quality score (0.0 - 10.0), higher is better
    """
    score = 0.0
    
    try:
        # 1. ATTRIBUTE QUALITY (string analysis - very fast)
        if title_attr and len(title_attr.strip()) > 3:
            score += 1.0
            
        class_str = (class_attr or '').lower()
        # Navigation/important classes boost score
        if any(nav_class in class_str for nav_class in ['nav', 'menu', 'primary', 'main', 'important']):
            score += 1.5
        # Marketing/ad classes reduce score  
        if any(bad_class in class_str for bad_class in ['ad', 'sponsor', 'track', 'promo', 'banner']):
            score -= 1.0
            
        rel_str = (rel_attr or '').lower()
        # Semantic rel values
        if any(good_rel in rel_str for good_rel in ['canonical', 'next', 'prev', 'chapter']):
            score += 1.0
        if any(bad_rel in rel_str for bad_rel in ['nofollow', 'sponsored', 'ugc']):
            score -= 0.5
            
        # 2. URL STRUCTURE QUALITY (string operations - very fast)
        url_lower = url.lower()
        
        # High-value path patterns
        if any(good_path in url_lower for good_path in ['/docs/', '/api/', '/guide/', '/tutorial/', '/reference/', '/manual/']):
            score += 2.0
        elif any(medium_path in url_lower for medium_path in ['/blog/', '/article/', '/post/', '/news/']):
            score += 1.0
            
        # Penalize certain patterns
        if any(bad_path in url_lower for bad_path in ['/admin/', '/login/', '/cart/', '/checkout/', '/track/', '/click/']):
            score -= 1.5
            
        # URL depth (shallow URLs often more important)
        url_depth = url.count('/') - 2  # Subtract protocol and domain
        if url_depth <= 2:
            score += 1.0
        elif url_depth > 5:
            score -= 0.5
            
        # HTTPS bonus
        if url.startswith('https://'):
            score += 0.5
            
        # 3. TEXT QUALITY (string analysis - very fast)
        if link_text:
            text_clean = link_text.strip()
            if len(text_clean) > 3:
                score += 1.0
                
            # Multi-word links are usually more descriptive
            word_count = len(text_clean.split())
            if word_count >= 2:
                score += 0.5
            if word_count >= 4:
                score += 0.5
                
            # Avoid generic link text
            generic_texts = ['click here', 'read more', 'more info', 'link', 'here']
            if text_clean.lower() in generic_texts:
                score -= 1.0
                
        # 4. CONTEXTUAL RELEVANCE (pre-computed page terms - very fast)
        if page_context.get('terms') and link_text:
            link_words = set(word.strip('.,!?;:"()[]{}').lower() 
                           for word in link_text.split() 
                           if len(word.strip('.,!?;:"()[]{}')) > 2)
            
            if link_words:
                # Calculate word overlap ratio
                overlap = len(link_words & page_context['terms'])
                if overlap > 0:
                    relevance_ratio = overlap / min(len(link_words), 10)  # Cap to avoid over-weighting
                    score += relevance_ratio * 2.0  # Up to 2 points for relevance
                    
        # 5. DOMAIN CONTEXT BONUSES (very fast string checks)
        if page_context.get('is_docs_site', False):
            # Documentation sites: prioritize internal navigation
            if link_text and any(doc_keyword in link_text.lower() 
                               for doc_keyword in ['api', 'reference', 'guide', 'tutorial', 'example']):
                score += 1.0
                
    except Exception:
        # Fail gracefully - return minimal score
        score = 0.5
        
    # Ensure score is within reasonable bounds
    return max(0.0, min(score, 10.0))
