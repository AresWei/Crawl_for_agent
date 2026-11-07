"""Local adaptation of Crawl4AI LLMContentFilter (822-1105).

Upstream commit 838869bdda93f25e6d50b3f50ae9577d9b19a586; Apache-2.0,
original attribution retained in LICENSE.crawl4ai / NOTICE. The source flow
(overlapping chunks, ordered filtering, usage and reusable results) is adapted
through local_extraction. Default output is verified source slices; explicit
transform mode emits separately labelled HTML-derived Markdown with source
references. No browser, server or GPU owner is added.
"""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit
import uuid

from ..core.paths import AppPaths
from ..intelligence.local_model import LocalModelClient
from .local_extraction import extract_local, _number


VERSION = "extractive-filter-v1"
TRANSFORM_VERSION = "html-markdown-transform-v1"
TRANSFORM_SCHEMA = {"type": "object", "properties": {
    "markdown": {"type": "string", "minLength": 1, "maxLength": 8000}},
    "required": ["markdown"], "additionalProperties": False}
TRANSFORM_INSTRUCTION = (
    "Convert the supplied HTML into clean Markdown in data.markdown. Preserve its facts, "
    "headings, lists, tables, and fenced code; omit navigation and adverts. You may regroup "
    "related material and normalize formatting, never invent facts. Keep link labels and "
    "remove tracking query parameters. Do not output HTML tags except inside code. Return "
    "one record per useful block, with evidence containing exact original HTML substrings "
    "supporting that block. Markdown is a model-derived transformation, not a verbatim quote. "
    "User relevance request: "
)
SCHEMA = {"type": "object", "properties": {"keep": {"const": True}},
          "required": ["keep"], "additionalProperties": False}
INSTRUCTION = (
    "Filter the source for the user's relevance request. Select useful original passages, "
    "omit navigation, advertisements and irrelevant boilerplate. Return one record per "
    "selected passage: data must be {\"keep\":true}, evidence must contain the EXACT "
    "original passage including its original punctuation and Markdown. Do not summarize, "
    "translate or rewrite. Retain enough context to avoid changing meaning. Return no "
    "records if no passage is relevant. User relevance request: "
)
_EXTRACTION_KEYS = {
    "max_chunks", "max_records", "max_tokens", "max_calls", "max_total_tokens",
    "max_chunk_chars", "max_source_chars", "max_seconds", "chunk_token_threshold",
    "overlap_rate", "word_token_rate", "apply_chunking",
}


def _hash(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _model_identity(root, model):
    """Bind cache to the actual managed worker and checkpoint, never just an alias.

    Worker restart, model/engine file replacement and config changes invalidate
    the key. File metadata avoids repeatedly reading a multi-GB checkpoint.
    Caller-injected clients must explicitly provide their own cache identity.
    """
    if model is not None and not isinstance(model, LocalModelClient):
        identity = getattr(model, "cache_identity", None)
        return identity if isinstance(identity, dict) and identity.get("model_id") else None
    try:
        state = json.loads((root / "runtime/model-server/state.json").read_text(encoding="utf-8-sig"))
        files = {}
        for key in ("model", "executable"):
            path = Path(state[key]).resolve()
            stat = path.stat()
            files[key] = [str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        from ..core.model_profiles import state_profile
        return {"model_id": state_profile(root, state)['alias'], "worker_state_sha256": _hash(state), "files": files}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(set(map(tuple, ranges))):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _render(result, text):
    if result["mode"] == "local_html_markdown_transform":
        blocks = result["derived_blocks"]
        result["derived_markdown"] = "\n\n".join(
            block["markdown"] + "\n\n[^source-" + str(index) + "]"
            for index, block in enumerate(blocks, 1))
        if blocks:
            # The marker points to the exact HTML evidence carried in citations;
            # no generated sentence is presented as a verified source quotation.
            result["derived_markdown"] += "\n\n" + "\n".join(
                f"[^source-{index}]: Original {result['source']['field']}; "
                f"citation source-{index} (see structured source_evidence)."
                for index in range(1, len(blocks) + 1))
        result["derived_html"] = "<pre>" + html.escape(result["derived_markdown"]) + "</pre>"
        result["rendering"] = {"derived_html": "escaped_model_derived_markdown_in_pre_element",
                               "derived_markdown": "model_blocks_with_original_html_citations",
                               "original_dom_structure_preserved": False}
        return result
    ranges = result["source_ranges"]
    result["fit_markdown"] = "\n\n".join(text[start:end] for start, end in ranges)
    # Safe display of exact Markdown text; deliberately not an original DOM view.
    result["fit_html"] = "\n".join("<pre>" + html.escape(text[start:end]) + "</pre>" for start, end in ranges)
    result["rendering"] = {"fit_html": "escaped_source_markdown_in_pre_elements",
                           "fit_markdown": "source_slices_joined_with_two_newlines",
                           "original_dom_structure_preserved": False}
    return result


def _clean_tracking_url(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return url
        parts = parsed.query.split("&")
        kept = []
        for part in parts:
            key = unquote_plus(part.split("=", 1)[0]).lower()
            if not key.startswith("utm_") and key not in {"gclid", "fbclid", "msclkid"}:
                kept.append(part)
        # Preserve all retained query segments byte-for-byte, including signed
        # values, duplicate keys, percent-escape spelling, ordering and blanks.
        return url if kept == parts else urlunsplit(parsed._replace(query="&".join(kept)))
    except ValueError:
        return url


_INLINE_LINK = re.compile(r"(\]\()(https?://[^\s()]+)(\))")
_CODE_SPAN = re.compile(r"(^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\2[ \t]*(?:\n|$)|(`+)[^\n]*?\3)", re.MULTILINE | re.DOTALL)


def _markdown_links(markdown):
    code = [match.span() for match in _CODE_SPAN.finditer(markdown)]
    return [match for match in _INLINE_LINK.finditer(markdown)
            if not any(start <= match.start() < end for start, end in code)]


def _clean_tracking_links(markdown):
    """Normalize HTTP(S) inline Markdown links without dropping functional queries."""
    for match in reversed(_markdown_links(markdown)):
        markdown = markdown[:match.start()] + match[1] + _clean_tracking_url(match[2]) + match[3] + markdown[match.end():]
    return markdown


def _link_provenance(text, url, markdown):
    """Map recognized output link destinations back to untouched HTML tags."""
    destinations = {match[2] for match in _markdown_links(markdown)}
    offsets, position = [], 0
    for line in text.splitlines(keepends=True):
        offsets.append(position)
        position += len(line)
    links = []
    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag != "a":
                return
            href = dict(attrs).get("href")
            if not isinstance(href, str):
                return
            destination = _clean_tracking_url(urljoin(url, href))
            if destination not in destinations:
                return
            line, column = self.getpos()
            start = offsets[line - 1] + column
            original = self.get_starttag_text()
            links.append({"output_url": destination, "original_href": href,
                          "original_tag": original, "source_range": [start, start + len(original)]})
    parser = Links(convert_charrefs=True)
    parser.feed(text)
    parser.close()
    return links, sorted(destinations - {item["output_url"] for item in links})


def _read_cache(path, key, ttl, text):
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry["key"] != key or time.time() - entry["stored_at"] > ttl:
            return None
        result = entry["result"]
        if result["status"] != "complete" or result["source"]["sha256"] != _hash(text):
            return None
        if any(not isinstance(span, list) or len(span) != 2 or
               any(type(i) is not int for i in span) or not 0 <= span[0] < span[1] <= len(text)
               for span in result["source_ranges"]):
            return None
        if result["mode"] == "local_html_markdown_transform":
            for block in result["derived_blocks"]:
                if not isinstance(block["markdown"], str) or not block["source_evidence"]:
                    return None
                for evidence in block["source_evidence"]:
                    start, end = evidence["source_range"]
                    if text[start:end] != evidence["text"] or evidence["source_field_sha256"] != _hash(text):
                        return None
        result["cached_usage"] = result["usage"]
        result["usage"] = {}
        result["budget"] = {"calls_used": 0, "output_tokens_reserved": 0}
        result["cache"] = {"status": "hit", "stored_at": entry["stored_at"], "reused_at": time.time()}
        return _render(result, text)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


async def filter_local(document: dict, url: str, config: dict, *, context=None,
                       model=None, identity_partition: str | None = None) -> dict:
    """Opt-in source filtering; never mutate the document or replace source fields.

    Context uses the existing extraction client/lock/output budget and shared GPU
    coordinator. Failed/truncated processing returns the full source as fallback.
    Cache requires an explicit hashed account partition supplied by PageCache;
    neither identities nor account/session values enter prompts or cache payloads.
    """
    if not isinstance(config, dict):
        raise ValueError("llm_content_filter must be an object")
    unknown = set(config) - (_EXTRACTION_KEYS | {"mode", "instruction", "cache", "cache_ttl_seconds"})
    if unknown:
        raise ValueError("Unsupported local content filter options: " + ", ".join(sorted(unknown)))
    instruction = config.get("instruction", "Keep the main substantive content.")
    if not isinstance(instruction, str) or len(instruction) > 900:
        raise ValueError("instruction must be at most 900 characters")
    use_cache = config.get("cache", False)
    if type(use_cache) is not bool:
        raise ValueError("cache must be boolean")
    ttl = _number(config, "cache_ttl_seconds", 3600, 0, 2592000)
    mode = config.get("mode", "extractive")
    if mode not in {"extractive", "transform"}:
        raise ValueError("mode must be extractive or transform")
    transform = mode == "transform"
    field = ("html" if isinstance(document.get("html"), str) else "cleaned_html") if transform else "markdown"
    text = document.get(field)
    if not isinstance(text, str):
        raise ValueError("Local content filtering requires the original " + field + " field")
    if context is not None:
        await context.check_control()
    root = context.paths.root if context is not None else getattr(model, "root", None)
    root = AppPaths.discover(root).root
    identity = _model_identity(root, model) if use_cache else None
    partition_valid = isinstance(identity_partition, str) and len(identity_partition) == 64 and all(
        c in "0123456789abcdef" for c in identity_partition)
    version = TRANSFORM_VERSION if transform else VERSION
    key = _hash([version, field, identity_partition, identity, config, url, _hash(text)]) if identity and partition_valid else None
    path = root / "cache/web-content-filter" / (key + ".json") if key else None
    started = time.monotonic()
    if path:
        cached = _read_cache(path, key, ttl, text)
        if cached is not None:
            cached["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
            return cached
    options = {key: value for key, value in config.items() if key in _EXTRACTION_KEYS}
    options.update(schema=TRANSFORM_SCHEMA if transform else SCHEMA,
                   instruction=(TRANSFORM_INSTRUCTION if transform else INSTRUCTION) + instruction,
                   input_format="html" if transform else "markdown")
    # Only selected source text enters the model; title framing and unrelated
    # document fields cannot become retained quotations accidentally.
    extracted = await extract_local({"cleaned_html" if transform else "markdown": text}, url, options,
                                    context=context, model=model)
    if transform:
        # extract_local's HTML port is named cleaned_html; remap its exact spans
        # to the actual untouched input field, which may be the original html.
        for record in extracted["records"]:
            for evidence in record["source_evidence"]:
                evidence["source_field"] = field
    selected = _merge_ranges([e["source_range"] for record in extracted["records"]
                              for e in record["source_evidence"]])
    fallback = extracted["status"] != "complete"
    result = {
        "status": extracted["status"], "mode": "local_html_markdown_transform" if transform else "local_extractive_content_filter", "version": version,
        "source": {"url": url, "field": field, "sha256": _hash(text), "characters": len(text)},
        "source_ranges": [[0, len(text)]] if fallback and text and not transform else selected,
        "selected_source_ranges": selected, "fallback_to_original": fallback,
        "selection_is_model_judgment": True, "semantic_accuracy_automatically_proven": False,
        "coverage": extracted["coverage"], "usage": extracted["usage"],
        "chunks": [{key: chunk[key] for key in ("index", "start", "end", "status", "errors", "usage", "finish_reason")
                    if key in chunk} for chunk in extracted["chunks"]],
        "budget": extracted["budget"], "limitations": extracted["limitations"],
        "model": extracted.get("model"), "latency_ms": extracted["latency_ms"],
        "cache": {"status": "miss" if path else "disabled"},
    }
    if transform:
        result.update(classification="model-derived", index_policy="excluded_from_original_source_index",
                      source_preserved=True, source_ranges_describe="supporting_original_html_evidence",
                      fallback_to_original=False, original_source_available=True)
        result["derived_blocks"] = [
            {"markdown": _clean_tracking_links(record["data"]["markdown"]),
             "classification": "model-derived", "citation_id": "source-" + str(index),
             "source_url": url, "source_evidence": record["source_evidence"]}
            for index, record in enumerate(extracted["records"], 1)]
        result["limitations"] = list(result["limitations"]) + [
            "Markdown is model-derived; exact HTML citations do not prove semantic equivalence.",
            "Tracking cleanup covers HTTP(S) inline Markdown links and known tracking keys; other link syntax may retain tracking.",
        ]
    _render(result, text)
    if transform:
        links, unmatched = _link_provenance(text, url, result["derived_markdown"])
        result["link_provenance"] = links
        result["unverified_link_destinations"] = unmatched
    if path and not fallback and result["model"] == identity["model_id"]:
        # Detect a worker/checkpoint change during generation; never publish that
        # result under the earlier model identity.
        if _model_identity(root, model) == identity:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
                temporary.write_text(json.dumps({"key": key, "stored_at": time.time(), "result": result},
                                                ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            except OSError:
                result["cache"]["write_status"] = "failed"
    if use_cache and not path:
        result["cache"]["reason"] = "identity_partition_or_actual_model_identity_unavailable"
    return result
