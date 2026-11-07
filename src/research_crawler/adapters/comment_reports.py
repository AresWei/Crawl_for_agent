"""Resumable local comment annotation and evidence-linked report jobs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import re
import time

from playwright.async_api import Error as BrowserError

from ..analysis.comments import (ANNOTATION_SCHEMA, LABEL_VERSION, PROMPT_VERSION, RULE_VERSION,
    model_messages, redact_text, rule_annotation, safe_url, unknown_labels, validate_annotation)
from ..analysis.reports import aggregate, report_document
from ..analysis import comment_units
from ..core.records import Record
from ..core.sessions import controlled, open_browser
from ..intelligence.local_model import LocalModelClient, ModelCompletion
from ..intelligence.text_client import text_client

CAPABILITIES = {"implemented": ["comment_emotion_intent_stance_aspect", "resumable_annotation",
    "deterministic_counts", "evidence_markdown_html_pdf"], "label_version": LABEL_VERSION,
    "paid_fallback": False, "modes": ["local_model", "rules"], "answer_backends": ["local", "codex"],
    "limitations": ["Labels are interpretations, not verified facts.", "No population representativeness claim.",
                    "Rules use literal cues only; semantic accuracy is not automatically proven."]}

_KEY = re.compile(r"[0-9a-f]{64}\Z")


def _latest_rows(store, job_id: str, kind: str) -> list[dict]:
    """Latest observed version per record, deterministic tie break; bounded selection."""
    with store.connection() as db:
        rows = db.execute("""SELECT * FROM (
            SELECT o.record_key,o.content_hash,o.observed_at,o.url,o.coverage_json,r.native_id,r.source,
                   v.payload_json,v.source_date,v.source_date_field,
                   ROW_NUMBER() OVER(PARTITION BY o.record_key ORDER BY o.observed_at DESC,o.content_hash DESC) rank
            FROM observations o JOIN records r ON r.key=o.record_key
            JOIN record_versions v ON v.record_key=o.record_key AND v.content_hash=o.content_hash
            WHERE o.job_id=? AND r.kind=?) WHERE rank=1 ORDER BY record_key LIMIT 10001""", (job_id, kind)).fetchall()
    if len(rows) > 10000:
        raise ValueError("Select a bounded source job of at most 10000 comments")
    return [dict(row) for row in rows]


def _evidence(row: dict, maximum: int) -> dict:
    payload = json.loads(row["payload_json"])
    raw = next((payload[name] for name in ("content", "text", "message") if isinstance(payload.get(name), str)), "")
    text, redacted = redact_text(raw)
    coverage = json.loads(row["coverage_json"])
    # No author identifiers, headers, account data or arbitrary source payloads enter the model/report.
    return {"record_key": row["record_key"], "content_hash": row["content_hash"], "native_id": str(row["native_id"]),
            "source": row["source"], "url": safe_url(row["url"]), "source_date": row["source_date"],
            "source_date_field": row["source_date_field"], "observed_at": row["observed_at"],
            "text": text[:maximum], "text_characters": len(raw), "text_truncated": len(text) > maximum,
            "credentials_redacted": redacted,
            "source_coverage": {name: coverage[name] for name in ("root_comments_only", "reply_collection", "content_truncated", "complete") if isinstance(coverage.get(name), (str, bool))}}


def _selection(context, maximum: int, text_maximum: int) -> tuple[list[dict], dict]:
    source_id = context.params.get("source_job_id")
    if not isinstance(source_id, str) or not source_id or source_id == context.job_id:
        raise ValueError("Supply a source comment job")
    source = context.store.job(source_id)
    rows = _latest_rows(context.store, source_id, "comment")
    keys = context.params.get("record_keys")
    if keys is not None:
        if not isinstance(keys, list) or not keys or len(keys) > 1000 or any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in keys):
            raise ValueError("record_keys must contain 1..1000 comment record keys")
        if len(set(keys)) != len(keys) or set(keys) - {row["record_key"] for row in rows}:
            raise ValueError("Every selected key must belong to a source comment")
        by_key = {row["record_key"]: row for row in rows}
        selected = [by_key[key] for key in keys]
    else:
        selected = rows
    selected = selected[:maximum]
    if not selected:
        raise ValueError("The source job contains no selected comment records")
    snapshot = [_evidence(row, text_maximum) for row in selected]
    full = source["status"] == "complete" and len(selected) == len(rows) and source["summary"].get("coverage_complete") is True
    coverage = {"source_job_id": source_id, "source_collection_status": source["status"], "available_comments": len(rows),
                "selected_comments": len(selected), "selection_limited": len(selected) < len(rows),
                "source_coverage_complete": full, "selection_order": "explicit_keys" if keys else "record_key_ascending"}
    return snapshot, coverage


def complete_mixed_quote_context(labels, evidence):
    """Expose the complete bounded input for mixed labels, without relabeling.

    Existing quote validation permits at most 240 characters. Longer or
    incomplete inputs retain the model quote and are explicitly incomplete;
    a full-context slice still does not prove the mixed interpretation.
    """
    labels = copy.deepcopy(labels)
    contexts = {}
    text = evidence['text']
    full_input = (not evidence.get('text_truncated', True)
                  and not evidence.get('credentials_redacted', True)
                  and evidence.get('text_characters') == len(text))
    for dimension in ('emotion', 'stance'):
        item = labels[dimension]
        if item['label'] != 'mixed':
            continue
        original_quote = item['quote']
        complete = full_input and bool(text.strip()) and len(text) <= 240
        context = {'method': 'complete-mixed-input-context-v1', 'status': 'complete_context' if complete else 'incomplete',
                   'original_model_quote': original_quote, 'complete_input_available': full_input,
                   'quote_limit_characters': 240, 'source_record_key': evidence['record_key'],
                   'source_content_hash': evidence['content_hash'], 'semantic_support_proven': False,
                   'label_judgment_changed': False, 'range': None}
        if complete:
            item['quote'] = text
            context.update(range={'start': 0, 'end': len(text), 'field': 'evidence.text',
                                  'unit': 'unicode_code_points', 'end_exclusive': True},
                           display_quote_changed=original_quote != text)
        else:
            context.update(display_quote_changed=False,
                           reason='incomplete_input' if not full_input else 'whole_comment_exceeds_quote_limit')
        contexts[dimension] = context
    return labels, contexts


def _annotation_record(context, evidence, labels, mode, status, reason, completion=None, *, stages=None, batch=None):
    contexts = {}
    if status == 'valid':
        labels, contexts = complete_mixed_quote_context(labels, evidence)
    payload = {"label_version": LABEL_VERSION, "method": mode,
        "method_version": PROMPT_VERSION if mode == "local_model" else RULE_VERSION,
        "source_job_id": context.params["source_job_id"], "stance_target": context.params.get("stance_target", ""),
        "annotation_status": status, "unknown_reason": reason, "labels": labels, "evidence": evidence,
        "interpretation_not_fact": True, "semantic_accuracy_automatically_proven": False,
        "model": completion.model if completion else None, "usage": completion.usage if completion else {},
        "latency_ms": completion.latency_ms if completion else 0,
        "finish_reason": completion.finish_reason if completion else None}
    if contexts:
        payload['evidence_context'] = contexts
    backend = context.params.get('answer_backend', 'local')
    payload.update(answer_backend=backend if mode == 'local_model' else None,
                   network_required=mode == 'local_model' and backend == 'codex',
                   account_quota_used=bool(completion) and backend == 'codex', api_key_used=False)
    if batch is not None:
        payload.update(batch_provenance=batch, usage_scope='batch_charged_once_at_first_item')
    if stages is not None:
        payload.update(method_version=comment_units.VERSION, analysis_pipeline="evidence_first",
                       analysis_stages=stages)
        payload["usage"] = {name: sum(stage.get("usage", {}).get(name, 0) for stage in stages)
                            for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
        payload["latency_ms"] = sum(stage.get("latency_ms", 0) for stage in stages)
    return Record("comment_analysis", "comment_annotation", f"{context.job_id}:{evidence['record_key']}", evidence["url"], payload,
        coverage={"generated": True, "source_record_key": evidence["record_key"], "source_content_hash": evidence["content_hash"],
                  "schema_and_quotes_validated": status == "valid", "interpretation_not_fact": True})


def _completion_finished(context, completion):
    return completion.finish_reason == ('completed' if context.params.get('answer_backend') == 'codex' else 'stop')


async def _codex_batch(context, model, snapshot, indices, target, revision):
    """One small production-prompt batch; persist before per-item validation.

    The analysis revision distinguishes an explicit retry from resuming after
    the batch response was saved but before annotation commit. Raw transport
    usage lives once in this receipt and is charged to only its first item.
    """
    inputs = [{'id': 'c' + str(index), 'record_key': snapshot[index]['record_key'],
               'content_hash': snapshot[index]['content_hash'],
               'text_sha256': hashlib.sha256(snapshot[index]['text'].encode()).hexdigest()}
              for index in indices]
    identity = {'inputs': inputs, 'target': target, 'prompt_version': PROMPT_VERSION,
                'backend': 'codex', 'analysis_revision': revision, 'batch_version': 'codex-production-batch-v1'}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    stream = 'comments.batch:' + key
    checkpoint = await context.checkpoint(stream)
    if checkpoint:
        receipt = checkpoint['metadata']['receipt']
    else:
        annotation = ANNOTATION_SCHEMA['json_schema']['schema']
        ids = [item['id'] for item in inputs]
        schema = {'type': 'json_schema', 'json_schema': {'name': 'comment_batch', 'strict': True,
            'schema': {'type': 'object', 'properties': {name: annotation for name in ids},
                       'required': ids, 'additionalProperties': False}}}
        system = model_messages('', target)[0]['content'] + (
            '\n本次是互相独立的评论小组。输出顶层对象，键必须恰好为输入的 id，'
            '每个值是遵守上述单条规范的完整标签对象。按 id 对应各自评论，'
            '不得将一条评论的观点或引句借给另一条，也不得将小组当成对话或相互上下文。')
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps({
            'stance_target': target or None,
            'comments': [{'id': item['id'], 'comment': snapshot[index]['text']}
                         for item, index in zip(inputs, indices)]}, ensure_ascii=False)}]
        completion = await controlled(context, model.complete(messages, max_tokens=1024,
                                                               temperature=0, response_format=schema))
        receipt = {'raw_output': completion.text, 'model': completion.model, 'usage': completion.usage,
                   'latency_ms': completion.latency_ms, 'finish_reason': completion.finish_reason,
                   'identity': getattr(model, 'cache_identity', None)}
        await context.commit_page(stream, [], next_cursor=None, exhausted=True,
                                  metadata={'inputs': identity, 'receipt': receipt})
    output = None
    try:
        value = json.loads(receipt['raw_output'])
        if (receipt['finish_reason'] != 'completed' or not isinstance(value, dict)
                or set(value) != {item['id'] for item in inputs}):
            raise ValueError('Incomplete or mismatched comment batch')
        output = value
    except (ValueError, TypeError):
        pass
    result = {}
    for offset, item in enumerate(inputs):
        completion = ModelCompletion(json.dumps(output[item['id']], ensure_ascii=False) if output is not None else '',
            receipt['model'], receipt['usage'] if offset == 0 else {},
            receipt['latency_ms'] if offset == 0 else 0, receipt['finish_reason'])
        result[item['id']] = (completion, {'checkpoint': stream, 'item_id': item['id'],
            'source_record_key': item['record_key'], 'source_content_hash': item['content_hash'],
            'batch_size': len(indices), 'usage_charged_here': offset == 0,
            'prompt_version': PROMPT_VERSION, 'batch_version': 'codex-production-batch-v1'})
    return result


async def _evidence_first(context, model, evidence, target, stages):
    """Persist valid extraction so explicit resume does not repeat stage one."""
    stream = "comments.units:" + evidence["record_key"]
    saved = await context.checkpoint(stream)
    if saved:
        first = saved["metadata"]
        if (first["version"] != comment_units.VERSION or first["source_hash"] != evidence["content_hash"]
                or first["text_sha256"] != hashlib.sha256(evidence["text"].encode()).hexdigest()):
            raise ValueError("Pinned comment extraction changed")
        receipt = dict(first["receipt"], reused=True)
        extracted = comment_units.validate_units(receipt["raw_output"], evidence["text"])
        stages.append(receipt)
    else:
        completion = await controlled(context, model.complete(comment_units.extraction_messages(evidence["text"]),
            max_tokens=1024, temperature=0, response_format=comment_units.UNIT_SCHEMA))
        receipt = {"stage": "extract", "version": comment_units.VERSION, "raw_output": completion.text,
                   "model": completion.model, "usage": completion.usage, "latency_ms": completion.latency_ms,
                   "finish_reason": completion.finish_reason, "reused": False}
        stages.append(receipt)
        if not _completion_finished(context, completion):
            raise ValueError("Incomplete extraction")
        extracted = comment_units.validate_units(completion.text, evidence["text"])
        if not extracted["complete"]:
            raise ValueError("Incomplete extraction coverage")
        receipt["extracted"] = extracted
        await context.commit_page(stream, [], next_cursor=None, exhausted=True,
            metadata={"version": comment_units.VERSION, "source_hash": evidence["content_hash"],
                      "text_sha256": hashlib.sha256(evidence["text"].encode()).hexdigest(), "receipt": receipt})
    await context.check_control()
    completion = await controlled(context, model.complete(
        comment_units.classification_messages(evidence["text"], target, extracted),
        max_tokens=640, temperature=0, response_format=comment_units.CLASSIFICATION_SCHEMA))
    receipt = {"stage": "classify", "version": comment_units.VERSION, "raw_output": completion.text,
               "model": completion.model, "usage": completion.usage, "latency_ms": completion.latency_ms,
               "finish_reason": completion.finish_reason, "reused": False}
    stages.append(receipt)
    if not _completion_finished(context, completion):
        raise ValueError("Incomplete classification")
    selected = comment_units.validate_classification(completion.text, evidence["text"], target, extracted)
    receipt["selection"] = selected
    if not selected["complete"]:
        raise ValueError("Incomplete classification coverage")
    return selected["labels"], completion


def _summary(context, coverage, failed, pending, processed):
    rows = _latest_rows(context.store, context.job_id, "comment_annotation")
    annotations = [json.loads(row["payload_json"]) for row in rows]
    complete = not failed and not pending and processed == coverage["selected_comments"]
    return {"status": "complete" if complete and coverage["source_coverage_complete"] else "partial",
            **coverage, "processing_complete": complete, "coverage_complete": complete and coverage["source_coverage_complete"],
            "processed_comments": len(annotations), "invalid_or_skipped_comments": len(failed),
            "counts": aggregate(annotations), "label_version": LABEL_VERSION,
            "method_versions": sorted({row["method_version"] for row in annotations}),
            "semantic_accuracy_automatically_proven": False, "interpretation_not_fact": True}


async def analyze_comments(context, *, model_factory=LocalModelClient):
    params = context.params
    mode = params.get("mode", "local_model")
    backend = params.get('answer_backend', 'local')
    if backend not in {'local', 'codex'} or (mode == 'rules' and backend != 'local'):
        raise ValueError('answer_backend must be local or codex for model annotations')
    pipeline = params.get("analysis_pipeline", "single_pass")
    if pipeline not in {"single_pass", "evidence_first"} or (pipeline == "evidence_first" and mode != "local_model"):
        raise ValueError("Invalid comment analysis pipeline")
    maximum, batch_size, text_maximum = int(params.get("max_comments", 100)), int(params.get("batch_size", 3 if backend == 'codex' else 1)), int(params.get("max_text_chars", 1000))
    target = params.get("stance_target", "")
    if mode not in {"local_model", "rules"} or not 1 <= maximum <= 1000 or not 1 <= batch_size <= 3 or not 100 <= text_maximum <= 1600:
        raise ValueError("Invalid annotation mode or budget")
    if not isinstance(target, str) or len(target) > 160 or redact_text(target)[1]:
        raise ValueError("Stance target must be at most 160 characters and contain no credentials")
    checkpoint = await context.checkpoint("comments.analyze")
    if checkpoint and checkpoint["metadata"].get("analysis_pipeline", "single_pass") != pipeline:
        raise ValueError("Pinned comment pipeline changed")
    if checkpoint and (checkpoint['metadata'].get('answer_backend', 'local') != backend
            or (backend == 'codex' and checkpoint['metadata'].get('model_batch_size') != batch_size)):
        raise ValueError('Pinned comment backend or model batch size changed')
    if checkpoint and checkpoint["exhausted"]:
        return dict(checkpoint["metadata"]["summary"])
    frozen = await context.checkpoint("comments.snapshot")
    if checkpoint:
        metadata = checkpoint["metadata"]
        snapshot = metadata.get("snapshot") or frozen["metadata"]["snapshot"]
        coverage = metadata["coverage"]
        cursor = checkpoint["cursor"]
        start, failed = cursor["next_index"], set(cursor["retry_indices"])
    else:
        if frozen:
            snapshot, coverage = frozen["metadata"]["snapshot"], frozen["metadata"]["coverage"]
        else:
            snapshot, coverage = await asyncio.to_thread(_selection, context, maximum, text_maximum)
        start, failed = 0, set()
    if not frozen:
        # Keep the frozen source once, rather than rewriting up to 1.6 MB every batch.
        await context.commit_page("comments.snapshot", [], next_cursor=None, exhausted=True,
            metadata={"snapshot": snapshot, "coverage": coverage})
    if not checkpoint:
        checkpoint = await context.commit_page("comments.analyze", [], next_cursor={"next_index": 0, "retry_indices": []},
            exhausted=False, metadata={"coverage": coverage, "analysis_pipeline": pipeline,
                'answer_backend': backend, 'model_batch_size': batch_size})
    plan = sorted(failed) + list(range(start, len(snapshot)))
    model = None
    for begin in range(0, len(plan), batch_size):
        await context.check_control()
        records = []
        batch_results = {}
        batch_indices = plan[begin:begin + batch_size]
        if backend == 'codex' and pipeline == 'single_pass':
            eligible = [index for index in batch_indices if not snapshot[index]['credentials_redacted']
                        and not snapshot[index]['text_truncated'] and snapshot[index]['text'].strip()]
            if eligible:
                if model is None:
                    client = text_client(context.paths.root, backend) if model_factory is LocalModelClient else model_factory(context.paths.root)
                    model = await context.resources.enter_async_context(client)
                batch_results = await _codex_batch(context, model, snapshot, eligible, target, checkpoint['revision'])
        for index in plan[begin:begin + batch_size]:
            await context.check_control()
            evidence = snapshot[index]
            labels, completion = unknown_labels(), None
            stages = [] if pipeline == "evidence_first" else None
            batch = None
            status, reason = "valid", None
            if evidence["credentials_redacted"] or evidence["text_truncated"] or not evidence["text"].strip():
                status = "skipped"
                reason = "credentials_redacted" if evidence["credentials_redacted"] else "oversized_text" if evidence["text_truncated"] else "empty_text"
            elif mode == "rules":
                labels = rule_annotation(evidence["text"])
            else:
                if model is None:
                    client = text_client(context.paths.root, backend) if model_factory is LocalModelClient else model_factory(context.paths.root)
                    model = await context.resources.enter_async_context(client)
                try:
                    if pipeline == "evidence_first":
                        labels, completion = await _evidence_first(context, model, evidence, target, stages)
                    else:
                        if backend == 'codex':
                            completion, batch = batch_results['c' + str(index)]
                        else:
                            completion = await controlled(context, model.complete(model_messages(evidence["text"], target),
                                max_tokens=640, temperature=0, response_format=ANNOTATION_SCHEMA))
                        if not _completion_finished(context, completion):
                            raise ValueError("Incomplete model output")
                        labels = validate_annotation(completion.text, evidence["text"], target)
                except (ValueError, TypeError):
                    status, reason = "invalid", "invalid_or_incomplete_evidence_pipeline" if stages is not None else "invalid_or_truncated_model_output"
            if status != "valid":
                failed.add(index)
            else:
                failed.discard(index)
                if any(labels[name]["label"] == "unknown" for name in ("emotion", "intent", "stance")):
                    reason = "insufficient_evidence_or_context"
            records.append(_annotation_record(context, evidence, labels, mode, status, reason, completion, stages=stages, batch=batch))
            start = max(start, index + 1)
        checkpoint = await context.commit_page("comments.analyze", records,
            next_cursor={"next_index": start, "retry_indices": sorted(failed)}, exhausted=False,
            expected_revision=checkpoint["revision"], metadata={"coverage": coverage, "analysis_pipeline": pipeline,
                'answer_backend': backend, 'model_batch_size': batch_size})
    summary = await asyncio.to_thread(_summary, context, coverage, failed, 0, start)
    await context.commit_page("comments.analyze", [], next_cursor={"next_index": start, "retry_indices": sorted(failed)},
        exhausted=not failed, expected_revision=checkpoint["revision"],
        metadata={"coverage": coverage, "analysis_pipeline": pipeline, 'answer_backend': backend,
                  'model_batch_size': batch_size, "summary": dict(summary)})
    if summary["counts"]["valid_annotations"] == 0:
        raise ValueError("No valid comment annotations were produced")
    return summary


def _artifact(path: Path, content_type: str) -> dict:
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "content_type": content_type}


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


async def _print_pdf(context, html: str, path: Path):
    # Shared lifecycle, empty identity, no scripts, network or external resources.
    browser = await open_browser(context, platform="report", storage_state={"cookies": [], "origins": []},
        context_options={"java_script_enabled": False, "offline": True}, manage_budget=False)
    await browser.context.route("**/*", lambda route: route.abort())
    await controlled(context, browser.page.set_content(html, wait_until="load"))
    temporary = path.with_suffix(".pdf.tmp")
    await controlled(context, browser.page.pdf(path=str(temporary), format="A4", print_background=True,
        margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"}))
    temporary.replace(path)


async def build_report(context, *, pdf_printer=_print_pdf):
    started = time.perf_counter()
    params = context.params
    source_id = params.get("source_job_id")
    formats = params.get("formats", ["markdown", "html", "pdf"])
    title = params.get("title", "评论样本分析报告")
    if not isinstance(source_id, str) or source_id == context.job_id:
        raise ValueError("Supply a completed or partially completed comment annotation job")
    if not isinstance(formats, list) or not formats or len(formats) > 3 or len(set(formats)) != len(formats) or set(formats) - {"markdown", "html", "pdf"}:
        raise ValueError("Formats must be unique markdown/html/pdf values")
    if not isinstance(title, str) or not 1 <= len(title) <= 120 or redact_text(title)[1]:
        raise ValueError("Invalid report title")
    source = await asyncio.to_thread(context.store.job, source_id)
    if source["kind"] != "comments.analyze" or source["status"] not in {"complete", "partial", "paused", "failed", "cancelled", "interrupted"}:
        raise ValueError("Report source must be a settled comment annotation job")
    checkpoint = await context.checkpoint("report.build")
    if checkpoint and checkpoint["exhausted"]:
        return dict(checkpoint["metadata"]["summary"])
    if checkpoint:
        annotations, coverage = checkpoint["metadata"]["annotations"], checkpoint["metadata"]["coverage"]
        source_status = checkpoint["metadata"]["source_status"]
        files = checkpoint["metadata"].get("files", [])
    else:
        rows = await asyncio.to_thread(_latest_rows, context.store, source_id, "comment_annotation")
        annotations = [json.loads(row["payload_json"]) for row in rows]
        if not annotations:
            raise ValueError("The annotation job contains no reportable evidence")
        source_checkpoint = await asyncio.to_thread(context.store.checkpoint, source_id, "comments.analyze")
        coverage = dict(source_checkpoint["metadata"].get("coverage", {})) if source_checkpoint else {}
        coverage.update({key: source["summary"][key] for key in ("processing_complete", "coverage_complete") if key in source["summary"]})
        source_status, files = source["status"], []
        checkpoint = await context.commit_page("report.build", [], next_cursor=None, exhausted=False,
            metadata={"annotations": annotations, "coverage": coverage, "source_status": source_status, "files": files})
    markdown, html, counts = report_document(annotations, source_job_id=source_id, source_status=source_status, coverage=coverage, title=title)
    folder = context.paths.run(context.job_id)
    folder.mkdir(parents=True, exist_ok=True)
    failures = []
    for format in formats:
        await context.check_control()
        extension, content_type = {"markdown": ("md", "text/markdown; charset=utf-8"), "html": ("html", "text/html; charset=utf-8"), "pdf": ("pdf", "application/pdf")}[format]
        path = folder / f"comment-report.{extension}"
        existing = next((item for item in files if item["path"] == str(path.resolve())), None)
        if existing and path.is_file() and _artifact(path, content_type) == existing:
            continue
        try:
            if format == "pdf":
                await pdf_printer(context, html, path)
            else:
                await asyncio.to_thread(_write_text, path, markdown if format == "markdown" else html)
            artifact = await asyncio.to_thread(_artifact, path, content_type)
        except (OSError, RuntimeError, ValueError, BrowserError) as error:
            failures.append({"format": format, "reason": type(error).__name__})
            continue
        files = [item for item in files if item["path"] != artifact["path"]] + [artifact]
        checkpoint = await context.commit_page("report.build", [], next_cursor=format, exhausted=False,
            expected_revision=checkpoint["revision"], metadata={"annotations": annotations, "coverage": coverage,
                "source_status": source_status, "files": files})
    summary = {"status": "complete" if not failures and coverage.get("coverage_complete") else "partial", "source_job_id": source_id,
        "source_status": source_status, "files": files, "format_failures": failures, "counts": counts,
        "processing_complete": len(files) == len(formats), "coverage_complete": bool(coverage.get("coverage_complete")),
        "source_coverage": coverage, "model_used_for_statistics": False, "interpretation_not_fact": True,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2), "artifact_bytes": sum(item["bytes"] for item in files)}
    record = Record("comment_analysis", "report", context.job_id, "", summary,
        coverage={"generated": True, "sample_size": counts["sample_size"], "coverage_complete": summary["coverage_complete"]})
    await context.commit_page("report.build", [record] if files else [], next_cursor=None, exhausted=not failures,
        expected_revision=checkpoint["revision"], metadata={"annotations": annotations, "coverage": coverage,
            "source_status": source_status, "files": files, "summary": dict(summary)})
    if not files:
        raise ValueError("No report artifact was produced")
    return summary


def register(runner):
    slot = asyncio.Semaphore(1)
    async def scheduled(context):
        async with slot:
            return await analyze_comments(context)
    async def handler(context):
        return await controlled(context, scheduled(context))
    runner.register("comments.analyze", handler)
    runner.register("report.build", build_report)
