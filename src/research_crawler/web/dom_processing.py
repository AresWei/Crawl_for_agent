# Derived in part from Crawl4AI commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and dom_assets/source-manifest.json.
# Local changes: shared-page ownership, cancellation, explicit coverage, budgets and host-driven steps.
"""Bounded source-derived DOM transforms over an already-owned Playwright page.

No browser/context/page is created or closed. Original source assets and every
modification are tracked in dom_assets/source-manifest.json.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from playwright.async_api import Error as BrowserError

ASSETS = Path(__file__).with_name("dom_assets")


def script(name):
    return (ASSETS / (name + ".js")).read_text(encoding="utf-8")


async def _controlled(awaitable, check_control):
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=.1)
            if check_control:
                await check_control()
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _capture(frame, max_bytes, max_nodes, flatten=False):
    serializer = script("flatten_shadow_dom") if flatten else "() => document.documentElement.outerHTML"
    expression = """(options) => {
        if (!document.documentElement) return {error: 'document_missing'};
        if (document.querySelectorAll('*').length > options.max_nodes) return {error: 'dom_node_budget'};
        const serialize = SERIALIZER;
        let html;
        try {html = serialize(options);} catch (error) {return {error: 'dom_node_budget'};}
        const bytes = new TextEncoder().encode(html).length;
        if (bytes > options.max_bytes) return {error: 'dom_byte_budget', bytes};
        let openRoots = 0;
        const inspect = root => { for (const node of root.querySelectorAll('*')) {
            if (node.shadowRoot) {openRoots++; inspect(node.shadowRoot);}
        }};
        inspect(document);
        return {html, bytes, open_shadow_roots: openRoots, base_url: document.baseURI};
    }""".replace("SERIALIZER", "(" + serializer + ")")
    return await frame.evaluate(expression, {"max_bytes": max_bytes, "max_nodes": max_nodes})


async def process_dom(page, params: dict | None = None, *, check_control=None) -> dict:
    """Return raw/transformed HTML and bounded-capture evidence.

    Params: process_iframes, flatten_shadow_dom, remove_overlay_elements,
    virtual_scroll_config (container_selector, scroll_count, scroll_by,
    wait_after_scroll, item_selector, item_key_attribute, max_items),
    max_html_bytes, dom_max_capture_bytes, dom_max_nodes, dom_max_seconds,
    iframe_max_depth, iframe_max_count, iframe_timeout_ms.

    check_control is an optional async callback (normally ctx.check_control).
    Cancellation/typed core control exceptions propagate; time/size/frame
    limits return explicit missing evidence, never an invented complete page.
    """
    params = params or {}
    maximum = max(1024, min(50_000_000, int(params.get("max_html_bytes", 5_000_000))))
    capture_maximum = max(maximum, min(100_000_000, int(params.get("dom_max_capture_bytes", maximum * 2))))
    max_nodes = max(100, min(100000, int(params.get("dom_max_nodes", 50000))))
    timeout = max(.1, min(120, float(params.get("dom_max_seconds", 30))))
    started = time.monotonic()
    missing, unknown, transformations = [], [], {}
    result = {"html": "", "raw_html": "", "coverage": {}, "transformations": transformations}
    captured_bytes = 0

    def miss(kind, reason, **details):
        missing.append({"kind": kind, "reason": reason, **details})

    async def run(awaitable):
        return await _controlled(awaitable, check_control)

    async def iframes(frame, depth=0):
        nonlocal captured_bytes
        maximum_depth = max(0, min(10, int(params.get("iframe_max_depth", 3))))
        maximum_count = max(1, min(100, int(params.get("iframe_max_count", 20))))
        data = transformations.setdefault("iframes", {"observed": 0, "captured": 0, "frames": []})
        handles = await run(frame.query_selector_all("iframe"))
        if handles and depth >= maximum_depth:
            miss("iframe", "depth_limit", depth=depth, count=len(handles))
            return
        for element in handles:
            if data["observed"] >= maximum_count:
                miss("iframe", "count_limit", limit=maximum_count)
                return
            index = data["observed"]
            data["observed"] += 1
            try:
                child = await run(element.content_frame())
                if child is None:
                    miss("iframe", "content_frame_unavailable", index=index)
                    continue
                await run(child.wait_for_load_state("domcontentloaded", timeout=max(100, min(10000, int(params.get("iframe_timeout_ms", 3000))))))
                source = child.url
                raw = await run(_capture(child, min(maximum, capture_maximum - captured_bytes), max_nodes))
                if raw.get("error"):
                    miss("iframe", raw["error"], index=index)
                    continue
                captured_bytes += raw["bytes"]
                await iframes(child, depth + 1)
                capture = await run(_capture(child, maximum, max_nodes, params.get("flatten_shadow_dom", False)))
                if capture.get("error"):
                    miss("iframe", capture["error"], index=index)
                    continue
                await run(element.evaluate(script("iframe_replace"), {"html": capture["html"], "source_url": source,
                    "base_url": capture["base_url"], "index": index}))
                data["frames"].append({"index": index, "depth": depth + 1, "source_url": source,
                    "raw_html": raw["html"], "source_sha256": hashlib.sha256(raw["html"].encode()).hexdigest(),
                    "open_shadow_roots": capture["open_shadow_roots"]})
                data["captured"] += 1
            except (BrowserError, TimeoutError):
                miss("iframe", "frame_detached_or_unavailable", index=index)

    async def virtual_scroll(config):
        nonlocal captured_bytes
        if not isinstance(config, dict) or not config.get("container_selector"):
            raise ValueError("virtual_scroll_config requires container_selector")
        count = max(0, min(200, int(config.get("scroll_count", 10))))
        wait = max(0, min(5, float(config.get("wait_after_scroll", .2))))
        options = {"container_selector": config["container_selector"], "scroll_by": config.get("scroll_by", "container_height"),
                   "item_selector": config.get("item_selector"), "item_key_attribute": config.get("item_key_attribute"),
                   "max_bytes": maximum, "max_items": max(1, min(10000, int(config.get("max_items", 2000))))}
        if isinstance(options["scroll_by"], (int, float)) and options["scroll_by"] <= 0:
            raise ValueError("virtual scroll_by must be positive")
        stats = {"scrolls": 0, "replaced": False, "reached_end": False, "chunks_count": 0, "unique_count": 0}
        transformations["virtual_scroll"] = stats
        js = script("virtual_scroll_steps")
        snapshot = await run(page.evaluate(js, {**options, "action": "snapshot"}))
        if snapshot.get("error"):
            miss("virtual_scroll", snapshot["error"])
            return
        previous = snapshot["html"]
        chunks = []
        chunk_bytes = 0
        for _ in range(count):
            await run(page.evaluate(js, {**options, "action": "scroll"}))
            await run(asyncio.sleep(wait))
            current = await run(page.evaluate(js, {**options, "action": "snapshot"}))
            if current.get("error"):
                miss("virtual_scroll", current["error"])
                break
            stats["scrolls"] += 1
            html = current["html"]
            # Original upstream classification: unchanged, append, or replacement.
            if html != previous and not html.startswith(previous):
                size = len(previous.encode())
                if captured_bytes + chunk_bytes + size > capture_maximum:
                    miss("virtual_scroll", "capture_byte_budget")
                    break
                chunks.append(previous)
                chunk_bytes += size
                stats["replaced"] = True
            previous = html
            stats["reached_end"] = bool(current["reached_end"])
            if stats["reached_end"]:
                break
        if not stats["reached_end"]:
            miss("virtual_scroll", "scroll_limit", limit=count)
        if chunks:
            # Unlike upstream, preserve the final observed chunk even at a budget stop.
            final_size = len(previous.encode())
            if captured_bytes + chunk_bytes + final_size > capture_maximum:
                miss("virtual_scroll", "capture_byte_budget")
                return
            chunks.append(previous)
            captured_bytes += chunk_bytes + final_size
            merged = await run(page.evaluate(js, {**options, "action": "merge", "chunks": chunks}))
            if merged.get("error"):
                miss("virtual_scroll", merged["error"])
            else:
                stats.update(merged)
        unknown.append("virtual_items_not_observed_in_dom")

    try:
        async with asyncio.timeout(timeout):
            raw = await run(_capture(page, maximum, max_nodes))
            if raw.get("error"):
                miss("document", raw["error"])
            else:
                result["raw_html"] = result["html"] = raw["html"]
                captured_bytes += raw["bytes"]
                if params.get("remove_overlay_elements"):
                    # Original close-button activation is opt-in; removal alone is the default.
                    transformations["overlays"] = await run(page.evaluate(script("remove_overlay_elements"),
                        {"click_close_buttons": bool(params.get("overlay_click_close_buttons", False))}))
                if params.get("virtual_scroll_config"):
                    await virtual_scroll(params["virtual_scroll_config"])
                if params.get("process_iframes"):
                    await iframes(page)
                flatten = bool(params.get("flatten_shadow_dom"))
                if flatten:
                    unknown.append("closed_shadow_roots_not_accessible")
                final = await run(_capture(page, maximum, max_nodes, flatten))
                if final.get("error"):
                    miss("document", final["error"])
                else:
                    result["html"] = final["html"]
                    if flatten:
                        transformations["shadow_dom"] = {"open_roots_serialized": final["open_shadow_roots"],
                            "closed_roots": "not_inspected_or_force_opened", "slot_resolution": True}
    except TimeoutError:
        miss("document", "dom_time_budget")
    except BrowserError:
        miss("document", "page_detached_or_script_error")
    result["coverage"] = {"complete": not missing, "scope": "captured_dom_with_requested_transforms", "website_complete": False,
        "missing": missing, "unknown": sorted(set(unknown)), "captured_bytes": captured_bytes,
        "html_bytes": len(result["html"].encode()), "elapsed_seconds": round(time.monotonic() - started, 3)}
    return result
