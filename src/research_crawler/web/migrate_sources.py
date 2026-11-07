"""Reproduce the bounded source migration; run from the project root.

Runtime never imports the upstream checkout or launches an upstream product.
Source slices preserve comments/docstrings; excluded methods are recorded.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UPSTREAM = ROOT / "upstream/crawl4ai/crawl4ai-838869bdda93f25e6d50b3f50ae9577d9b19a586"
DEST = Path(__file__).parent
HEADER = "# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.\n# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.\n# Local modifications: source selection and application-local dependency imports.\n"
manifest = []


def save(relative, output, target=None, changes="Verbatim source with attribution header"):
    source = UPSTREAM / relative
    target = target or Path(relative).name
    path = DEST / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output, encoding="utf-8")
    manifest.append({"source": relative, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                     "target": str(path.relative_to(DEST)).replace("\\", "/"),
                     "target_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "changes": changes})


def selected(relative, names, imports, cut=None):
    source = (UPSTREAM / relative).read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    chunks = []
    for node in ast.parse(source).body:
        if getattr(node, "name", None) not in names:
            continue
        end = node.end_lineno
        if cut and node.name in cut:
            method = next(m for m in node.body if getattr(m, "name", None) == cut[node.name])
            end = min([method.lineno] + [d.lineno for d in method.decorator_list]) - 1
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        chunks.append("".join(lines[start - 1:end]))
    return HEADER + "from __future__ import annotations\n" + imports + "\n\n" + "\n\n".join(chunks)


def main():
    for path in sorted((UPSTREAM / "crawl4ai/html2text").glob("*.py")):
        save(str(path.relative_to(UPSTREAM)).replace("\\", "/"), HEADER + path.read_text(encoding="utf-8"),
             "html2text/" + path.name)
    source = (UPSTREAM / "crawl4ai/markdown_generation_strategy.py").read_text(encoding="utf-8")
    # Upstream's fast absolute-path join uses the entire base path; RFC urljoin fixes it.
    tree = ast.parse(source)
    join = next(n for n in tree.body if getattr(n, "name", None) == "fast_urljoin")
    lines = source.splitlines(keepends=True)
    source = "".join(lines[:join.lineno-1]) + 'def fast_urljoin(base: str, url: str) -> str:\n    return urljoin(base, url)\n' + "".join(lines[join.end_lineno:])
    save("crawl4ai/markdown_generation_strategy.py", HEADER + source,
         changes="Application-local result dataclass/filter imports; fix root-relative citation URL joining")
    filtered = selected("crawl4ai/content_filter_strategy.py", {"RelevantContentFilter", "PruningContentFilter"},
        "from abc import ABC, abstractmethod\nfrom typing import List, Tuple, Dict, Optional\nfrom collections import deque\nfrom bs4 import BeautifulSoup, Tag, NavigableString, Comment\nimport re, math, warnings\n")
    save("crawl4ai/content_filter_strategy.py", filtered,
         changes="Retain deterministic base and pruning filter; exclude BM25 and paid/LLM filter dependencies; postponed logger annotations")
    save("crawl4ai/content_filter_strategy_lxml.py", HEADER + (UPSTREAM / "crawl4ai/content_filter_strategy_lxml.py").read_text(encoding="utf-8"))
    extraction = selected("crawl4ai/extraction_strategy.py",
        {"ExtractionStrategy", "NoExtractionStrategy", "JsonElementExtractionStrategy", "JsonCssExtractionStrategy", "JsonLxmlExtractionStrategy", "JsonXPathExtractionStrategy", "RegexExtractionStrategy"},
        "from abc import ABC, abstractmethod\nfrom typing import Any, List, Dict, Optional, Tuple, Pattern, Union\nfrom concurrent.futures import ThreadPoolExecutor, as_completed\nfrom enum import IntFlag, auto\nfrom bs4 import BeautifulSoup\nfrom lxml import html, etree\nimport re, json\n",
        {"JsonElementExtractionStrategy": "_validate_schema", "RegexExtractionStrategy": "generate_pattern"})
    # The deterministic regex constructor uses this helper from the original module.
    source = (UPSTREAM / "crawl4ai/extraction_strategy.py").read_text(encoding="utf-8")
    start = source.index("_CTRL =")
    end = source.index("class RegexExtractionStrategy", start)
    extraction = extraction.replace("class RegexExtractionStrategy", source[start:end] + "class RegexExtractionStrategy", 1)
    save("crawl4ai/extraction_strategy.py", extraction,
         changes="Retain CSS/XPath hierarchical schema, no-op and regex extraction; exclude schema/pattern generation, LLM and embedding strategies")
    chunking = selected("crawl4ai/chunking_strategy.py",
        {"ChunkingStrategy", "IdentityChunking", "RegexChunking", "FixedLengthWordChunking", "SlidingWindowChunking", "OverlappingWindowChunking"},
        "from abc import ABC, abstractmethod\nimport re\n")
    save("crawl4ai/chunking_strategy.py", chunking, changes="Retain deterministic chunkers; exclude NLTK/model-loading chunkers")
    save("crawl4ai/utils.py", selected("crawl4ai/utils.py", {"clean_tokens"}, "import re\n"), "filter_utils.py", "Select deterministic upstream stop-word and noise filtering")
    save("crawl4ai/content_filter_strategy.py", selected("crawl4ai/content_filter_strategy.py", {"BM25ContentFilter"},
        "from typing import List\nfrom bs4 import BeautifulSoup\nfrom rank_bm25 import BM25Okapi\nfrom snowballstemmer import stemmer\nfrom .content_filter_strategy import RelevantContentFilter\nfrom .filter_utils import clean_tokens\n"), "bm25_filter.py", "Preserve BM25 implementation in a lazily imported module")
    save("crawl4ai/table_extraction.py", selected("crawl4ai/table_extraction.py", {"TableExtractionStrategy", "DefaultTableExtraction", "NoTableExtraction"},
        "from abc import ABC, abstractmethod\nfrom typing import Dict, List, Optional, Any, Union, Tuple\nfrom lxml import etree\nimport re\n"), "table_extraction.py", "Preserve deterministic table scoring, rowspan and colspan extraction without eager LLM dependencies")
    save("crawl4ai/utils.py", selected("crawl4ai/utils.py", {"extract_metadata", "normalize_url", "is_external_url", "get_base_domain", "extract_metadata_using_lxml", "extract_page_context", "calculate_link_intrinsic_score"},
        "import re\nimport lxml.html\nfrom bs4 import BeautifulSoup\nfrom urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode\n"), "scraping_utils.py", "Select metadata, URL and link scoring algorithms without network/model dependencies")
    source = (UPSTREAM / "crawl4ai/config.py").read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    constant_names = {"MIN_WORD_THRESHOLD", "IMAGE_DESCRIPTION_MIN_WORD_THRESHOLD", "IMAGE_SCORE_THRESHOLD", "ONLY_TEXT_ELIGIBLE_TAGS", "IMPORTANT_ATTRS", "SOCIAL_MEDIA_DOMAINS"}
    constants = ["".join(lines[n.lineno-1:n.end_lineno]) for n in ast.parse(source).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constant_names for t in n.targets)]
    save("crawl4ai/config.py", HEADER + "\n".join(constants), "scraping_config.py", "Select only deterministic scraper defaults; omit remote-provider/process defaults")
    save("crawl4ai/models.py", selected("crawl4ai/models.py", {"_coerce_int", "MediaItem", "Link", "Media", "Links", "ScrapingResult"},
        "from typing import Optional, Annotated, Dict, Any, List\nfrom pydantic import BaseModel, BeforeValidator\n"), "scraping_models.py", "Select original scraper result models")
    source = (UPSTREAM / "crawl4ai/content_scraping_strategy.py").read_text(encoding="utf-8")
    source = source.replace("import requests\n", "").replace("from requests.exceptions import InvalidSchema\n", "")
    source = source.replace("from .config import", "from .scraping_config import").replace("from .utils import", "from .scraping_utils import").replace("from .models import", "from .scraping_models import")
    tree, lines = ast.parse(source), source.splitlines(keepends=True)
    fn = next(n for n in tree.body if getattr(n, "name", None) == "fetch_image_file_size")
    source = "".join(lines[:fn.lineno-1]) + 'def fetch_image_file_size(img, base_url):\n    raise RuntimeError("Image metadata requests must use the shared media manager")\n' + "".join(lines[fn.end_lineno:])
    source = source.replace('link_preview_config = kwargs.get("link_preview_config")', 'link_preview_config = None  # Shared async pipeline owns all link-preview requests.')
    save("crawl4ai/content_scraping_strategy.py", HEADER + source, changes="Preserve LXML scraper; inject local model/config/utils modules; disable unused requests-owned image helper and independent link-preview transport")
    fingerprint = selected("crawl4ai/utils.py", {"compute_head_fingerprint"}, "import re, hashlib\n")
    fingerprint = fingerprint.replace("xxhash.xxh64(combined.encode()).hexdigest()", "hashlib.sha256(combined.encode()).hexdigest()")
    save("crawl4ai/utils.py", fingerprint, "cache_fingerprint.py", "Preserve fingerprint signals; use standard-library SHA256 instead of xxhash, so fingerprints are product-local")
    source = (UPSTREAM / "crawl4ai/cache_validator.py").read_text(encoding="utf-8")
    source = source.replace("from .egress_policy import proxy_url", "").replace("from .utils import compute_head_fingerprint", "from .cache_fingerprint import compute_head_fingerprint")
    source = source.replace("def __init__(self, timeout: float = 10.0, user_agent: Optional[str] = None):", "def __init__(self, client, timeout: float = 10.0, user_agent: Optional[str] = None):")
    source = source.replace("self._client: Optional[httpx.AsyncClient] = None", "self._client = client")
    tree, lines = ast.parse(source), source.splitlines(keepends=True)
    cls = next(n for n in tree.body if getattr(n, "name", None) == "CacheValidator")
    replacements = []
    for name, replacement in {
        "_get_client": "    async def _get_client(self):\n        return self._client\n",
        "close": "    async def close(self):\n        pass  # Shared JobContext owns the injected client.\n",
    }.items():
        method = next(n for n in cls.body if getattr(n, "name", None) == name)
        replacements.append((method.lineno-1, method.end_lineno, replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    source = "".join(lines).replace("except Exception as e:", "except (ValueError, OSError) as e:").replace("except Exception:", "except (httpx.HTTPError, ValueError):")
    source = source.replace("                chunks = []\n", '                chunks = []\n                content = b""  # Empty successful responses have no validation evidence.\n')
    save("crawl4ai/cache_validator.py", HEADER + source, changes="Inject shared HTTP client, no independent lifecycle; propagate core cancellation/budgets; preserve fresh/stale/unknown/error semantics; treat empty validation bodies as unavailable evidence")
    for relative in ("cache_context.py", "deep_crawling/scorers.py", "processors/pdf/processor.py", "processors/pdf/utils.py"):
        source = (UPSTREAM / "crawl4ai" / relative).read_text(encoding="utf-8")
        source = source.replace("Install with 'pip install crawl4ai[pdf]'", "Enable the research-crawler pdf optional dependency in its F-drive runtime")
        save("crawl4ai/" + relative, HEADER + source, relative)
    for name in ("base_strategy", "bfs_strategy", "dfs_strategy", "bff_strategy"):
        relative = "crawl4ai/deep_crawling/" + name + ".py"
        source = (UPSTREAM / relative).read_text(encoding="utf-8")
        source = "\n".join(line for line in source.splitlines() if not line.startswith("from ..types import") and line != "from __future__ import annotations")
        source = source.replace("from ..models import TraversalStats", "from .source_models import TraversalStats").replace("from ..models import CrawlResult", "")
        source = source.replace("from ..utils import", "from .source_utils import").replace("from . import DeepCrawlStrategy", "from .base_strategy import DeepCrawlStrategy")
        save(relative, HEADER + "from __future__ import annotations\n" + source + "\n", "deep_crawling/" + name + ".py",
             "Preserve upstream traversal and discovery algorithms; postpone annotations and use local source models/utilities. Product execution uses durable core frontier, not upstream callbacks as persistence.")
    source = (UPSTREAM / "crawl4ai/deep_crawling/filters.py").read_text(encoding="utf-8").replace("from ..utils import HeadPeekr", "from .support import HeadPeekr")
    # Upstream collects bare coroutines before a later synchronous rejection.
    # Close those unstarted coroutines so filtering cannot leak runtime warnings.
    source = source.replace("elif not result:  # Sync rejection", "elif not result:  # Sync rejection\n                for pending in tasks:\n                    pending.close()")
    save("crawl4ai/deep_crawling/filters.py", HEADER + source, "deep_crawling/filters.py",
         "Inject job-scoped HeadPeekr transport; close unstarted async filter coroutines on synchronous rejection")
    save("crawl4ai/models.py", selected("crawl4ai/models.py", {"TraversalStats"}, "from dataclasses import dataclass\nfrom datetime import datetime\n"), "deep_crawling/source_models.py", "Select upstream traversal statistics dataclass")
    save("crawl4ai/utils.py", selected("crawl4ai/utils.py", {"normalize_url_for_deep_crawl", "efficient_normalize_url_for_deep_crawl"}, "from functools import lru_cache\nfrom urllib.parse import urlparse, urlunparse\n"), "deep_crawling/source_utils.py", "Select upstream deep-crawl URL normalization")
    for directory in ("deep_crawling", "processors", "processors/pdf"):
        (DEST / directory / "__init__.py").write_text("\"\"\"Internal source-integrated algorithms; shared core owns resources.\"\"\"\n", encoding="utf-8")
    save("LICENSE", (UPSTREAM / "LICENSE").read_text(encoding="utf-8"), "LICENSE.crawl4ai", "Verbatim upstream license")
    # write_text translates line endings on Windows; legal text must remain byte-identical.
    (DEST / "LICENSE.crawl4ai").write_bytes((UPSTREAM / "LICENSE").read_bytes())
    manifest[-1]["target_sha256"] = hashlib.sha256((DEST / "LICENSE.crawl4ai").read_bytes()).hexdigest()
    (DEST / "source-manifest.json").write_text(json.dumps({"upstream_commit": "838869bdda93f25e6d50b3f50ae9577d9b19a586", "files": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
