"""Sitemap/robots selection adapted from frozen Crawl4AI seeder/domain mapper.

Copyright Crawl4AI contributors, Apache-2.0; see LICENSE.crawl4ai.
Changes: bounded gzip inflation, strict XML, explicit child manifests and lastmod
provenance; recursion and network requests are moved into durable shared jobs.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from lxml import etree

from ..search.remote import canonical_url
from .source_algorithms import Soft404Fingerprint, SourceAlgorithms, _parse_head


def decode_xml(content: bytes, maximum: int = 4_000_000):
    if content.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
            content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError("decompressed_byte_budget")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("xml_external_entities_disallowed")
    parser = etree.XMLParser(recover=False, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError("invalid_xml") from exc
    return content, root


def parse_sitemap(content: bytes, source_url: str, maximum: int = 4_000_000) -> dict:
    _, root = decode_xml(content, maximum)
    tag = etree.QName(root).localname
    if tag not in {"sitemapindex", "urlset"}:
        raise ValueError("unexpected_sitemap_document")
    # The namespace-agnostic child XPath and loc normalization are retained
    # from AsyncUrlSeeder._iter_sitemap_content. Return children, do not fetch.
    kind = "sitemap" if tag == "sitemapindex" else "url"
    entries = []
    for parent in root.xpath(f"./*[local-name()='{kind}']"):
        nodes = parent.xpath("./*[local-name()='loc']")
        if not nodes or not nodes[0].text:
            continue
        cleaned = nodes[0].text.strip().replace("\u200b", "").replace("\ufeff", "")
        try:
            url = canonical_url(urljoin(source_url, cleaned))
        except ValueError:
            continue
        entry = {"url": url}
        for field in ("lastmod", "changefreq", "priority"):
            values = parent.xpath(f"./*[local-name()='{field}']/text()")
            if values:
                entry[field] = values[0].strip()
        entries.append(entry)
    return {"kind": tag, "entries": entries}


def parse_robots(document: str, base_url: str) -> dict:
    sitemaps, directives = [], []
    agents = []
    # Retain concrete Allow/Disallow path mining from DomainMapper, while
    # preserving directive and user-agent instead of treating a denial as access.
    previous = None
    for raw in document.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            if not line:
                agents, previous = [], None
            continue
        name, value = [part.strip() for part in line.split(":", 1)]
        name = name.lower()
        if name == "user-agent":
            if previous != "user-agent":
                agents = []
            agents.append(value)
        elif name == "sitemap" and value:
            sitemaps.append(urljoin(base_url, value))
        elif name in {"allow", "disallow"} and value and "*" not in value and "$" not in value and len(value) > 1:
            directives.append({"url": urljoin(base_url, value), "directive": name, "user_agents": list(agents), "path": value})
        previous = name
    return {"sitemaps": list(dict.fromkeys(sitemaps)), "directives": directives}


def parse_feed(content: bytes, source_url: str) -> list[dict]:
    decoded, root = decode_xml(content)
    if etree.QName(root).localname not in {"rss", "feed", "RDF"}:
        raise ValueError("unexpected_feed_document")
    urls = SourceAlgorithms()._parse_feed_xml(decoded.decode("utf-8", errors="replace"), source_url)
    return [{"url": url} for url in dict.fromkeys(urls)]


def parse_homepage(document: str, source_url: str) -> tuple[list[dict], list[str]]:
    soup = BeautifulSoup(document, "lxml")
    entries, feeds = [], []
    for anchor in soup.select("a[href]"):
        target = urljoin(source_url, anchor["href"])
        entries.append({"url": target, "link_text": anchor.get_text(" ", strip=True)[:300]})
    head = _parse_head(document)
    for rel, links in head.get("link", {}).items():
        for link in links:
            url = urljoin(source_url, link.get("href", ""))
            if rel == "alternate" and any(kind in link.get("type", "").lower() for kind in ("rss", "atom")):
                feeds.append(url)
            elif rel in ("alternate", "preload", "prefetch", "next", "prev"):
                entries.append({"url": url, "head_rel": rel})
    return entries, feeds


def parse_cc(document: str) -> list[dict]:
    entries = []
    for line in document.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_common_crawl_jsonl") from exc
        if not isinstance(value, dict) or not isinstance(value.get("url"), str):
            raise ValueError("invalid_common_crawl_record")
        if value.get("status", "200") != "200":
            continue
        entries.append({"url": value["url"], "archive_timestamp": value.get("timestamp"),
                        "archive_digest": value.get("digest"), "archive_mime": value.get("mime"),
                        "archive_status": value.get("status")})
    return entries


def fingerprint(status: int, content: bytes) -> Soft404Fingerprint:
    body = content[:2048]
    match = re.search(rb"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    return Soft404Fingerprint(status, match.group(1).decode("utf-8", errors="replace").strip() if match else None,
                              len(content), hashlib.md5(body).hexdigest())
