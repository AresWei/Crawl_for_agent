"""Evidence-preserving HTML pipeline over migrated deterministic algorithms."""
import json
from dataclasses import asdict
from urllib.parse import urljoin, urlsplit, urldefrag

from bs4 import BeautifulSoup, Comment
from lxml import html as lhtml

from . import chunking_strategy as chunks
from .content_filter_strategy_lxml import PruningContentFilterLXML
from .content_scraping_strategy import LXMLWebScrapingStrategy
from .extraction_strategy import JsonCssExtractionStrategy, JsonLxmlExtractionStrategy, JsonXPathExtractionStrategy, RegexExtractionStrategy
from .markdown_generation_strategy import DefaultMarkdownGenerator
from .table_extraction import DefaultTableExtraction


def normalize_url(value):
    value = urldefrag(value.strip())[0]
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    return value


def extract_document(document, url, params):
    soup = BeautifulSoup(document, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    base = urljoin(url, soup.base.get("href", "")) if soup.base else url
    metadata = {str(m.get("name") or m.get("property")): m.get("content", "")
                for m in soup.find_all("meta") if m.get("name") or m.get("property")}
    metadata["title"] = title
    metadata["language"] = soup.html.get("lang", "") if soup.html else ""
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical:
        metadata["canonical"] = urljoin(base, canonical.get("href", ""))
    structured = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            structured.append(json.loads(tag.get_text()))
        except ValueError:
            pass
    links, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        try:
            target = normalize_url(urljoin(base, anchor["href"]))
        except ValueError:
            continue
        external = urlsplit(target).netloc != urlsplit(url).netloc
        if params.get("exclude_external_links") and external:
            continue
        if any(urlsplit(target).hostname == domain or (urlsplit(target).hostname or "").endswith("." + domain)
               for domain in params.get("exclude_domains", [])):
            continue
        if target not in seen:
            links.append({"url": target, "href": target, "text": anchor.get_text(" ", strip=True),
                          "title": anchor.get("title", ""), "rel": anchor.get("rel", []), "external": external})
            seen.add(target)
    if params.get("prefetch"):
        return {"title": title, "html": document, "links": links, "metadata": metadata, "prefetch": True,
                "markdown": "", "media": {"images": [], "videos": [], "audios": []}, "tables": []}
    media = {"images": [], "videos": [], "audios": []}
    for tag, kind in (("img", "images"), ("video", "videos"), ("audio", "audios")):
        for node in soup.find_all(tag):
            src = node.get("src") or node.get("data-src") or ""
            if params.get("exclude_external_images") and kind == "images" and urlsplit(urljoin(base, src)).netloc != urlsplit(url).netloc:
                continue
            item = {"src": urljoin(base, src) if src else "", "alt": node.get("alt", ""),
                    "title": node.get("title", ""), "width": node.get("width"), "height": node.get("height")}
            item["sources"] = [{"src": urljoin(base, source.get("src", "")), "type": source.get("type", "")}
                               for source in node.find_all("source", src=True)]
            if node.get("poster"):
                item["poster"] = urljoin(base, node["poster"])
            if node.get("srcset"):
                item["srcset"] = [{"src": urljoin(base, candidate.strip().split()[0]),
                                   "descriptor": " ".join(candidate.strip().split()[1:])}
                                  for candidate in node["srcset"].split(",") if candidate.strip()]
            media[kind].append(item)
    # Preserve title-prefixed raw fragments, which lxml may place entirely in head.
    if soup.body is None and soup.head is not None:
        soup.head.unwrap()
    for node in soup.find_all(string=lambda text: isinstance(text, Comment)):
        node.extract()
    exclusions = ["script", "style", "noscript", "template", *params.get("excluded_tags", [])]
    if params.get("remove_forms"):
        exclusions.append("form")
    selectors = params.get("excluded_selectors", [])
    if isinstance(selectors, str):
        selectors = [selectors]
    for selector in exclusions + selectors:
        for node in soup.select(selector):
            node.decompose()
    selected = soup.select(params["css_selector"]) if params.get("css_selector") else [soup.body or soup]
    cleaned = "\n".join(str(node) for node in selected)
    if params.get("only_text"):
        cleaned = "\n".join(node.get_text(" ", strip=True) for node in selected)
    # Retain upstream scraping semantics and its scored media/link result fields.
    scrape_options = dict(params.get("scraping_options", {}))
    for key in ("word_count_threshold", "image_score_threshold", "image_description_min_word_threshold", "remove_forms",
                "excluded_tags", "exclude_external_links", "exclude_external_images", "exclude_all_images", "exclude_domains",
                "exclude_social_media_links", "exclude_social_media_domains", "keep_data_attributes", "only_text", "score_links"):
        if key in params:
            scrape_options[key] = params[key]
    scrape_options.setdefault("word_count_threshold", 1)
    table_options = params.get("table_options", {})
    if params.get("extract_tables", True):
        scrape_options["table_extraction"] = DefaultTableExtraction(**table_options)
    # The complete source document remains in html. Filtered input is only a view.
    scraped = LXMLWebScrapingStrategy().scrap(base, "<html><body>" + cleaned + "</body></html>", **scrape_options)
    if not scraped.success:
        raise ValueError("HTML scraping failed")
    cleaned = scraped.cleaned_html
    source_links = scraped.links.model_dump()
    media_candidates, media = media, scraped.media.model_dump()
    tables = media.pop("tables", [])
    score_by_url = {item["href"]: item for group in source_links.values() for item in group}
    for link in links:
        link.update({k: v for k, v in score_by_url.get(link["url"], {}).items() if v is not None})
    content_filter = None
    filter_config = params.get("content_filter", {})
    if filter_config.get("type") == "bm25":
        from .bm25_filter import BM25ContentFilter
        content_filter = BM25ContentFilter(**{k: v for k, v in filter_config.items() if k != "type"})
    elif params.get("prune", True):
        options = {k: v for k, v in filter_config.items() if k != "type"}
        options.setdefault("threshold", float(params.get("prune_threshold", .48)))
        content_filter = PruningContentFilterLXML(**options)
    source = params.get("markdown_source", "cleaned_html")
    if source not in {"raw_html", "cleaned_html", "fit_html"}:
        raise ValueError("Unknown markdown_source")
    markdown_input = document if source == "raw_html" else cleaned
    if source == "fit_html" and content_filter:
        markdown_input = "\n".join(content_filter.filter_content(cleaned))
    markdown = DefaultMarkdownGenerator(content_filter=content_filter, options=params.get("markdown_options", {})).generate_markdown(
        markdown_input, base_url=base, citations=params.get("citations", True))
    if markdown.raw_markdown.startswith("Error "):
        raise ValueError("Markdown conversion failed")
    extracted = None
    if params.get("schema"):
        choices = {"css": JsonCssExtractionStrategy, "lxml": JsonLxmlExtractionStrategy, "xpath": JsonXPathExtractionStrategy}
        kind = params.get("schema_type", "css").lower()
        if kind not in choices:
            raise ValueError("Unknown schema_type")
        extracted = choices[kind](params["schema"]).extract(base, document)
    entities = RegexExtractionStrategy(custom=params["regex_patterns"]).extract(url, markdown.raw_markdown) if params.get("regex_patterns") else None
    chunk_config = params.get("chunking", {"type": "fixed", "chunk_size": max(1, int(params.get("chunk_words", 300)))})
    for key in ("chunk_size", "window_size", "step"):
        if key in chunk_config and not 1 <= int(chunk_config[key]) <= 100000:
            raise ValueError("Chunk sizes and steps must be positive and bounded")
    if chunk_config.get("type") == "overlapping" and not 0 <= int(chunk_config.get("overlap", 100)) < int(chunk_config.get("window_size", 1000)):
        raise ValueError("Chunk overlap must be smaller than its window")
    chunkers = {"identity": chunks.IdentityChunking, "regex": chunks.RegexChunking, "fixed": chunks.FixedLengthWordChunking,
                "sliding": chunks.SlidingWindowChunking, "overlapping": chunks.OverlappingWindowChunking}
    if chunk_config.get('type') in {'nlp_sentence','nlp_topic'}:
        # NLP runs in the shared cancellable CPU worker after source capture.
        # Keep source text here; the async enrichment replaces the chunks.
        chunk_config={'type':'identity'}
    if chunk_config.get("type", "fixed") not in chunkers:
        raise ValueError("Unknown chunking strategy")
    chunker = chunkers[chunk_config.get("type", "fixed")](**{k: v for k, v in chunk_config.items() if k != "type"})
    chunked = chunker.chunk(markdown.raw_markdown)
    from .nlp_chunking import source_ranges
    return {"title": title, "html": document, "cleaned_html": cleaned, **asdict(markdown),
            "markdown": markdown.raw_markdown, "links": links, "metadata": metadata, "json_ld": structured,
            "media": media, "media_candidates": media_candidates, "tables": tables, "extracted": extracted, "entities": entities,
            "chunks": chunked, "chunk_source_ranges":source_ranges(markdown.raw_markdown,chunked), "base_url": base}
