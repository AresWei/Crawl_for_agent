"""Reproduce source JS selections from the retained Crawl4AI checkout.

No upstream import, installation, browser or network access is performed.
"""
import ast
import hashlib
import json
from pathlib import Path

DEST = Path(__file__).resolve().parent
ROOT = DEST.parents[3]
UPSTREAM = ROOT / "upstream/crawl4ai/crawl4ai-838869bdda93f25e6d50b3f50ae9577d9b19a586"
HEADER = "// Derived from Crawl4AI commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.\n// Copyright Crawl4AI contributors. See ../LICENSE.crawl4ai and source-manifest.json.\n"


def main():
    entries = []
    for name in ("flatten_shadow_dom", "remove_overlay_elements"):
        source_path = "crawl4ai/js_snippet/" + name + ".js"
        source = (UPSTREAM / source_path).read_text(encoding="utf-8")
        changes = "Attribution header; runtime wrapper enforces node and byte limits."
        if name == "flatten_shadow_dom":
            source = source.replace("(() => {", "((options = {}) => {\n    let visited = 0;\n    const checkNode = () => {if (++visited > (options.max_nodes || 100000)) throw new Error('dom_node_budget');};\n    const escapeText = node => /^(SCRIPT|STYLE)$/.test(node.parentElement?.tagName || '') ? node.textContent : node.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');")
            source = source.replace("if (node.nodeType === Node.TEXT_NODE) return node.textContent;", "checkNode();\n        if (node.nodeType === Node.TEXT_NODE) return escapeText(node);")
            source = source.replace("})()", "})")
            changes = "Retain recursive open-shadow and slot serialization; escape ordinary text correctly; accept max_nodes; do not force-open closed roots."
        else:
            source = source.replace("async () => {", "async (options = {}) => {\n    const before = document.querySelectorAll('*').length;\n    const deadline = Date.now() + 3000;\n    let clicks = 0;")
            source = source.replace("for (const selector of commonSelectors.slice(0, 6))", "if (options.click_close_buttons === true) for (const selector of commonSelectors.slice(0, 6))")
            source = source.replace("if (isVisible(button)) {", "if (isVisible(button) && clicks < 10 && Date.now() < deadline) {\n                clicks++;")
            source = source.replace("document.body.scrollIntoView(false);", "document.body.scrollIntoView(false);\n    return {removed_elements: before - document.querySelectorAll('*').length, clicked_close_buttons: options.click_close_buttons === true};")
            changes = "Preserve source overlay removal heuristics; close-button clicking is explicit opt-in; return transformation counts. No consent-accept script is invoked."
        target = DEST / (name + ".js")
        target.write_text(HEADER + source, encoding="utf-8")
        entries.append({"source": source_path, "source_sha256": hashlib.sha256((UPSTREAM / source_path).read_bytes()).hexdigest(),
                        "target": target.name, "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "changes": changes})
    strategy = UPSTREAM / "crawl4ai/async_crawler_strategy.py"
    tree = ast.parse(strategy.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if getattr(n, "name", None) == "AsyncPlaywrightCrawlerStrategy")
    method = next(n for n in cls.body if getattr(n, "name", None) == "_handle_virtual_scroll")
    assignment = next(n for n in ast.walk(method) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "virtual_scroll_js" for t in n.targets))
    target = DEST / "virtual_scroll_original.js"
    target.write_text(HEADER + ast.literal_eval(assignment.value).strip() + "\n", encoding="utf-8")
    entries.append({"source": "crawl4ai/async_crawler_strategy.py", "source_sha256": hashlib.sha256(strategy.read_bytes()).hexdigest(),
                    "source_lines": [assignment.lineno, assignment.end_lineno], "target": target.name,
                    "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "changes": "Exact selected original virtual-scroll JS retained as reference. Runtime steps split this algorithm for cancellation, limits and Unicode-safe deduplication."})
    for target_name, method_name, changes in (
        ("../dom_processing.py", "_handle_virtual_scroll", "Adapt source virtual-scroll and process_iframes orchestration to an existing shared page; host-driven cancellation/time/byte/node/frame limits; return raw/derived evidence and explicit missing coverage."),
        ("virtual_scroll_steps.js", "_handle_virtual_scroll", "Adapt original snapshot/append/replacement/chunk merge algorithm into finite host-driven operations; preserve Unicode and stable IDs; retain final snapshot; enforce byte/item limits; clone the merged container to prevent live virtualizer listeners discarding the derived aggregate."),
        ("iframe_replace.js", "process_iframes", "Adapt original DOMParser and iframe-to-div replacement using a function argument and existing handle; preserve original ID/source and resolve relative URLs without interpolated JavaScript."),
    ):
        target = DEST / target_name
        method = next(n for n in cls.body if getattr(n, "name", None) == method_name)
        entries.append({"source": "crawl4ai/async_crawler_strategy.py", "source_sha256": hashlib.sha256(strategy.read_bytes()).hexdigest(),
                        "source_lines": [method.lineno, method.end_lineno], "target": target_name,
                        "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "changes": changes})
    (DEST / "source-manifest.json").write_text(json.dumps({"upstream_commit": "838869bdda93f25e6d50b3f50ae9577d9b19a586", "files": entries}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
