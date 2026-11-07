# Selected from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors; Apache-2.0. See LICENSE.crawl4ai and source-manifest.json.
# Pure algorithms retained; HTTP/cache/concurrency lifecycle belongs to shared core.
from __future__ import annotations
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from lxml import etree, html as lxml_html
LXML = True
try:
    import rank_bm25
    HAS_BM25 = True
except ImportError:
    HAS_BM25 = False

_meta_rx = re.compile(
    r'<meta\s+(?:[^>]*?(?:name|property|http-equiv)\s*=\s*["\']?([^"\' >]+)[^>]*?content\s*=\s*["\']?([^"\' >]+)[^>]*?)\/?>',
    re.I)

_charset_rx = re.compile(r'<meta\s+[^>]*charset=["\']?([^"\' >]+)', re.I)

_title_rx = re.compile(r'<title>(.*?)</title>', re.I | re.S)

_link_rx = re.compile(
    r'<link\s+[^>]*rel=["\']?([^"\' >]+)[^>]*href=["\']?([^"\' >]+)', re.I)

def _match(url: str, pattern: str) -> bool:
    if fnmatch.fnmatch(url, pattern):
        return True
    canon = url.split("://", 1)[-1]
    return (fnmatch.fnmatch(canon, pattern)
            or (canon.startswith("www.") and fnmatch.fnmatch(canon[4:], pattern)))

def _parse_head(src: str) -> Dict[str, Any]:
    if LXML:
        try:
            if isinstance(src, str):
                # strip Unicode, let lxml decode
                src = src.encode("utf-8", "replace")
            doc = lxml_html.fromstring(src)
        except (ValueError, etree.ParserError):
            return {}        # malformed, bail gracefully
        info: Dict[str, Any] = {
            "title": (doc.find(".//title").text or "").strip()
            if doc.find(".//title") is not None else None,
            "charset": None,
            "meta": {}, "link": {}, "jsonld": []
        }
        for el in doc.xpath(".//meta"):
            k = el.attrib.get("name") or el.attrib.get(
                "property") or el.attrib.get("http-equiv")
            if k:
                info["meta"][k.lower()] = el.attrib.get("content", "")
            elif "charset" in el.attrib:
                info["charset"] = el.attrib["charset"].lower()
        for el in doc.xpath(".//link"):
            rel_attr = el.attrib.get("rel", "")
            if not rel_attr:
                continue
            # Handle multiple space-separated rel values
            rel_values = rel_attr.lower().split()
            entry = {a: el.attrib[a] for a in (
                "href", "as", "type", "hreflang") if a in el.attrib}
            # Add entry for each rel value
            for rel in rel_values:
                info["link"].setdefault(rel, []).append(entry)
        # Extract JSON-LD structured data
        for script in doc.xpath('.//script[@type="application/ld+json"]'):
            if script.text:
                try:
                    jsonld_data = json.loads(script.text.strip())
                    info["jsonld"].append(jsonld_data)
                except json.JSONDecodeError:
                    pass
        # Extract html lang attribute
        html_elem = doc.find(".//html")
        if html_elem is not None:
            info["lang"] = html_elem.attrib.get("lang", "")
        return info
    # regex fallback
    info: Dict[str, Any] = {"title": None, "charset": None,
                            "meta": {}, "link": {}, "jsonld": [], "lang": ""}
    m = _title_rx.search(src)
    info["title"] = m.group(1).strip() if m else None
    for k, v in _meta_rx.findall(src):
        info["meta"][k.lower()] = v
    m = _charset_rx.search(src)
    info["charset"] = m.group(1).lower() if m else None
    for rel, href in _link_rx.findall(src):
        info["link"].setdefault(rel.lower(), []).append({"href": href})
    # Try to extract JSON-LD with regex
    jsonld_pattern = re.compile(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
    for match in jsonld_pattern.findall(src):
        try:
            jsonld_data = json.loads(match.strip())
            info["jsonld"].append(jsonld_data)
        except json.JSONDecodeError:
            pass
    # Try to extract lang attribute
    lang_match = re.search(r'<html[^>]*lang=["\']?([^"\' >]+)', src, re.I)
    if lang_match:
        info["lang"] = lang_match.group(1)
    return info

_NONSENSE_SUFFIXES = (
    "/robots.txt", "/sitemap.xml", "/sitemap_index.xml",
    "/favicon.ico", "/apple-touch-icon.png", "/browserconfig.xml",
    "/manifest.json", "/ads.txt", "/humans.txt",
    "/crossdomain.xml",
)

_NONSENSE_CONTAINS = (
    "/.well-known/", "/.git/", "/.svn/", "/.hg/",
)

_ASSET_EXTENSIONS = (
    ".js", ".css", ".scss", ".sass", ".less", ".map",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".avif",
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".bz2",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)

@dataclass
class Soft404Fingerprint:
    """Fingerprint of a known-bad URL response for soft-404 detection."""
    status_code: int
    title: Optional[str]
    content_length: int
    body_hash: str

class SourceAlgorithms:
    def _log(self, *args, **kwargs):
        pass

    def _extract_text_context(self, head_data: Dict[str, Any]) -> str:
        """Extract all relevant text from head metadata for scoring."""
        # Priority fields with their weights (for future enhancement)
        text_parts = []

        # Title
        if head_data.get("title"):
            text_parts.append(head_data["title"])

        # Standard meta tags
        meta = head_data.get("meta", {})
        for key in ["description", "keywords", "author", "subject", "summary", "abstract"]:
            if meta.get(key):
                text_parts.append(meta[key])

        # Open Graph tags
        for key in ["og:title", "og:description", "og:site_name", "article:tag"]:
            if meta.get(key):
                text_parts.append(meta[key])

        # Twitter Card tags
        for key in ["twitter:title", "twitter:description", "twitter:image:alt"]:
            if meta.get(key):
                text_parts.append(meta[key])

        # Dublin Core tags
        for key in ["dc.title", "dc.description", "dc.subject", "dc.creator"]:
            if meta.get(key):
                text_parts.append(meta[key])

        # JSON-LD structured data
        for jsonld in head_data.get("jsonld", []):
            if isinstance(jsonld, dict):
                # Extract common fields from JSON-LD
                for field in ["name", "headline", "description", "abstract", "keywords"]:
                    if field in jsonld:
                        if isinstance(jsonld[field], str):
                            text_parts.append(jsonld[field])
                        elif isinstance(jsonld[field], list):
                            text_parts.extend(str(item)
                                              for item in jsonld[field] if item)

                # Handle @graph structures
                if "@graph" in jsonld and isinstance(jsonld["@graph"], list):
                    for item in jsonld["@graph"]:
                        if isinstance(item, dict):
                            for field in ["name", "headline", "description"]:
                                if field in item and isinstance(item[field], str):
                                    text_parts.append(item[field])

        # Combine all text parts
        return " ".join(filter(None, text_parts))

    def _calculate_url_relevance_score(self, query: str, url: str) -> float:
        """Calculate relevance score between query and URL using string matching."""
        # Normalize inputs
        query_lower = query.lower()
        url_lower = url.lower()

        # Extract URL components
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.replace('www.', '')
        path = parsed.path.strip('/')

        # Create searchable text from URL
        # Split domain by dots and path by slashes
        domain_parts = domain.split('.')
        path_parts = [p for p in path.split('/') if p]

        # Include query parameters if any
        query_params = parsed.query
        param_parts = []
        if query_params:
            for param in query_params.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    param_parts.extend([key, value])

        # Combine all parts
        all_parts = domain_parts + path_parts + param_parts

        # Calculate scores
        scores = []
        query_tokens = query_lower.split()

        # 1. Exact match in any part (highest score)
        for part in all_parts:
            part_lower = part.lower()
            if query_lower in part_lower:
                scores.append(1.0)
            elif part_lower in query_lower:
                scores.append(0.9)

        # 2. Token matching
        for token in query_tokens:
            token_scores = []
            for part in all_parts:
                part_lower = part.lower()
                if token in part_lower:
                    # Score based on how much of the part the token covers
                    coverage = len(token) / len(part_lower)
                    token_scores.append(0.7 * coverage)
                elif part_lower in token:
                    coverage = len(part_lower) / len(token)
                    token_scores.append(0.6 * coverage)

            if token_scores:
                scores.append(max(token_scores))

        # 3. Character n-gram similarity (for fuzzy matching)
        def get_ngrams(text, n=3):
            return set(text[i:i+n] for i in range(len(text)-n+1))

        # Combine all URL parts into one string for n-gram comparison
        url_text = ' '.join(all_parts).lower()
        if len(query_lower) >= 3 and len(url_text) >= 3:
            query_ngrams = get_ngrams(query_lower)
            url_ngrams = get_ngrams(url_text)
            if query_ngrams and url_ngrams:
                intersection = len(query_ngrams & url_ngrams)
                union = len(query_ngrams | url_ngrams)
                jaccard = intersection / union if union > 0 else 0
                scores.append(0.5 * jaccard)

        # Calculate final score
        if not scores:
            return 0.0

        # Weighted average with bias towards higher scores
        scores.sort(reverse=True)
        weighted_score = 0
        total_weight = 0
        for i, score in enumerate(scores):
            weight = 1 / (i + 1)  # Higher weight for better matches
            weighted_score += score * weight
            total_weight += weight

        final_score = weighted_score / total_weight if total_weight > 0 else 0
        return min(final_score, 1.0)  # Cap at 1.0

    def _calculate_bm25_score(self, query: str, documents: List[str]) -> List[float]:
        """Calculate BM25 scores for documents against a query."""
        if not HAS_BM25:
            self._log(
                "warning", "rank_bm25 not installed. Returning zero scores.", tag="URL_SEED")
            return [0.0] * len(documents)

        if not query or not documents:
            return [0.0] * len(documents)

        # Tokenize query and documents (simple whitespace tokenization)
        # For production, consider using a proper tokenizer
        query_tokens = query.lower().split()
        tokenized_docs = [doc.lower().split() for doc in documents]

        # Handle edge case where all documents are empty
        if all(len(doc) == 0 for doc in tokenized_docs):
            return [0.0] * len(documents)

        # Create BM25 instance and calculate scores
        try:
            from rank_bm25 import BM25Okapi
            bm25 = BM25Okapi(tokenized_docs)
            scores = bm25.get_scores(query_tokens)

            # Normalize scores to 0-1 range
            # BM25 can return negative scores, so we need to handle the full range
            if len(scores) == 0:
                return []
            
            min_score = min(scores)
            max_score = max(scores)
            
            # If all scores are the same, return 0.5 for all
            if max_score == min_score:
                return [0.5] * len(scores)
            
            # Normalize to 0-1 range using min-max normalization
            normalized_scores = [(score - min_score) / (max_score - min_score) for score in scores]

            return normalized_scores
        except Exception as e:
            self._log("error", "Error calculating BM25 scores: {error}",
                      params={"error": str(e)}, tag="URL_SEED")
            return [0.0] * len(documents)

    def _is_soft_404(
        self, status_code: int, body: bytes, fingerprint: Optional[Soft404Fingerprint]
    ) -> bool:
        """Check if a response matches the soft-404 fingerprint."""
        if fingerprint is None:
            return False
        if status_code != 200:
            return False

        # Check body hash
        body_hash = hashlib.md5(body[:2048]).hexdigest()
        if body_hash == fingerprint.body_hash:
            return True

        # Check title
        m = re.search(rb"<title[^>]*>(.*?)</title>", body[:2048], re.I | re.S)
        if m and fingerprint.title:
            title = m.group(1).decode("utf-8", "replace").strip()
            if title == fingerprint.title:
                return True

        return False

    def _parse_feed_xml(self, xml_text: str, base_url: str) -> List[str]:
        """Parse RSS or Atom feed XML to extract entry URLs."""
        urls: List[str] = []
        try:
            if LXML:
                parser = etree.XMLParser(recover=False, resolve_entities=False, no_network=True)
                root = etree.fromstring(xml_text.encode("utf-8", "replace"), parser=parser)

                # RSS: <item><link>URL</link></item>
                for link in root.xpath("//*[local-name()='item']/*[local-name()='link']"):
                    if link.text and link.text.strip():
                        urls.append(urljoin(base_url, link.text.strip()))

                # Atom: <entry><link href="URL"/></entry>
                for link in root.xpath("//*[local-name()='entry']/*[local-name()='link']"):
                    href = link.get("href")
                    if href:
                        urls.append(urljoin(base_url, href.strip()))

                # RSS: <item><guid isPermaLink="true">URL</guid></item>
                if not urls:
                    for guid in root.xpath("//*[local-name()='item']/*[local-name()='guid']"):
                        is_permalink = guid.get("isPermaLink", "true").lower()
                        if is_permalink == "true" and guid.text and guid.text.strip().startswith("http"):
                            urls.append(guid.text.strip())
            else:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xml_text)
                for elem in root.iter():
                    if "}" in elem.tag:
                        elem.tag = elem.tag.split("}")[1]

                for item in root.findall(".//item"):
                    link = item.find("link")
                    if link is not None and link.text:
                        urls.append(urljoin(base_url, link.text.strip()))

                for entry in root.findall(".//entry"):
                    link = entry.find("link")
                    if link is not None:
                        href = link.get("href")
                        if href:
                            urls.append(urljoin(base_url, href.strip()))
        except Exception:
            pass
        return urls

    def _is_nonsense(self, url: str) -> bool:
        """Reduced nonsense filter for DomainMapper."""
        parsed = urlparse(url)
        path = parsed.path.lower()

        # Nonsense suffixes
        if any(path.endswith(s) for s in _NONSENSE_SUFFIXES):
            return True

        # Nonsense contains
        if any(c in path for c in _NONSENSE_CONTAINS):
            return True

        # Sitemap variations
        if "/sitemap" in path and path.endswith((".xml", ".xml.gz", ".txt")):
            return True

        # Static assets (JS, CSS, fonts, images, media, archives)
        if any(path.endswith(ext) for ext in _ASSET_EXTENSIONS):
            return True

        # Next.js / webpack chunks
        if "/_next/" in path or "/webpack" in path:
            return True

        # Hidden dotfiles
        parts = path.split("/")
        if any(p.startswith(".") and p not in (".", "..") for p in parts if p):
            return True

        # Wayback garbage: URLs with encoded newlines/backslashes
        if any(x in url for x in ("%5C", "%0A", "%0D", "\\n", "\\r")):
            return True

        return False
