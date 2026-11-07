"""Reproduce selected pure algorithms from the project's frozen Crawl4AI source."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

COMMIT = "838869bdda93f25e6d50b3f50ae9577d9b19a586"


def migrate(root: Path):
    target = root / "src/research_crawler/discovery"
    upstream = root / "upstream/crawl4ai" / ("crawl4ai-" + COMMIT)
    source_root = upstream / "crawl4ai"
    selections = {
        "async_url_seeder.py": {"globals": ["_meta_rx", "_charset_rx", "_title_rx", "_link_rx", "_match", "_parse_head"],
            "class": "AsyncUrlSeeder", "methods": ["_extract_text_context", "_calculate_url_relevance_score", "_calculate_bm25_score"]},
        "domain_mapper.py": {"globals": ["_NONSENSE_SUFFIXES", "_NONSENSE_CONTAINS", "_ASSET_EXTENSIONS", "Soft404Fingerprint"],
            "class": "DomainMapper", "methods": ["_parse_feed_xml", "_is_soft_404", "_is_nonsense"]},
    }
    header = '''# Selected from Crawl4AI 0.9.5, commit %s.
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

''' % COMMIT
    fragments, methods, manifest = [], [], []
    for filename, selection in selections.items():
        source = source_root / filename
        text = source.read_text(encoding="utf-8")
        tree = ast.parse(text)
        symbols = []
        for node in tree.body:
            name = node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else node.targets[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) else ""
            if name in selection["globals"]:
                start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
                fragments.append("\n".join(text.splitlines()[start - 1:node.end_lineno]))
                symbols.append(name)
            if isinstance(node, ast.ClassDef) and node.name == selection["class"]:
                for method in node.body:
                    if isinstance(method, ast.FunctionDef) and method.name in selection["methods"]:
                        segment = "\n".join(text.splitlines()[method.lineno - 1:method.end_lineno])
                        # Network/entity resolution is not needed for feed parsing.
                        segment = segment.replace("etree.XMLParser(recover=True)", "etree.XMLParser(recover=False, resolve_entities=False, no_network=True)")
                        methods.append(segment)
                        symbols.append(node.name + "." + method.name)
        manifest.append({"source": "crawl4ai/" + filename,
                         "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "symbols": symbols})
    contents = header + "\n\n".join(fragments) + "\n\nclass SourceAlgorithms:\n    def _log(self, *args, **kwargs):\n        pass\n\n" + "\n\n".join(methods) + "\n"
    output = target / "source_algorithms.py"
    output.write_text(contents, encoding="utf-8", newline="\n")
    (target / "LICENSE.crawl4ai").write_bytes((upstream / "LICENSE").read_bytes())
    (target / "source-manifest.json").write_text(json.dumps({"upstream_commit": COMMIT, "sources": manifest,
        "target": "source_algorithms.py", "target_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "adapted_targets": [{"target": "parsers.py", "target_sha256": hashlib.sha256((target / "parsers.py").read_bytes()).hexdigest(),
                             "derived_from": ["AsyncUrlSeeder._iter_sitemap_content", "DomainMapper._scan_robots_txt", "DomainMapper._fingerprint_soft_404", "DomainMapper._scan_homepage"]},
                            {"target": "archives.py", "target_sha256": hashlib.sha256((target / "archives.py").read_bytes()).hexdigest(),
                             "derived_from": ["DomainMapper._discover_via_crt", "DomainMapper._discover_via_wayback", "AsyncUrlSeeder._from_cc"],
                             "changes": "Retain certificate-name and archive-URL selection; public API page-count/resume protocol, exact host scopes, strict response validation and shared transport replace independent clients and silent failures."}],
        "changes": ["Select pure methods, preserve their bodies and algorithm behavior", "Move selected methods into one SourceAlgorithms class with silent logger", "Harden feed XML parser: no recovery, entities or network", "parsers.py adapts sitemap XPath/normalization and robots directive extraction from these same files", "Shared bounded driver replaces independent HTTP/cache/concurrency and recursive task scheduling"]}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    migrate(Path(__file__).resolve().parents[3])
