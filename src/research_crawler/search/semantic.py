"""Bounded local CPU semantic retrieval with version-exact source citations."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, closing
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from ..core.exclusive import lease
from ..core.records import Record, canonical_json
from ..core.sessions import controlled
from ..web.semantic_runtime import SemanticRuntime
from .local import SOURCE_FILTER_ARGS, SOURCE_FILTER_SQL, TEXT_FIELDS, _parameters


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _integer(params, key, default, lower, upper):
    value = params.get(key, default)
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{key} must be an integer in {lower}..{upper}')
    return value


def _corpus(store, params):
    maximum = _integer(params, 'max_records', 100, 1, 1000)
    max_chunks = _integer(params, 'max_chunks', 128, 1, 255)
    size = _integer(params, 'chunk_chars', 400, 80, 2000)
    overlap = _integer(params, 'overlap_chars', min(40, size - 1), 0, size - 1)
    source_job = params.get('source_job_id')
    if source_job is not None and (not isinstance(source_job, str) or not source_job):
        raise ValueError('source_job_id must be a nonempty job identifier')
    with store.connection() as db:
        if source_job is not None and db.execute('SELECT 1 FROM jobs WHERE id=?', (source_job,)).fetchone() is None:
            raise ValueError('source_job_id does not exist')
        # Reuse the lexical source eligibility, but select observed versions in
        # the requested job even if a later global version has no text fields.
        condition = ' AND o.job_id=?' if source_job else ''
        arguments = [*SOURCE_FILTER_ARGS, *([source_job] if source_job else [])]
        query = f'''SELECT * FROM (
            SELECT r.key AS record_key,v.content_hash,v.payload_json,r.source,r.kind,r.native_id,
                o.url,o.observed_at,o.coverage_json,o.job_id AS source_job_id,
                v.source_date,v.source_date_field,
                ROW_NUMBER() OVER(PARTITION BY r.key ORDER BY o.observed_at DESC,o.rowid DESC) AS n
            FROM records r JOIN observations o ON o.record_key=r.key
            JOIN record_versions v ON v.record_key=o.record_key AND v.content_hash=o.content_hash
            WHERE {SOURCE_FILTER_SQL}{condition}) WHERE n=1
            AND COALESCE(json_extract(payload_json,'$.source_usable'),1) != 0
            AND COALESCE(json_extract(payload_json,'$.blocking.exclude_from_answers'),0) != 1'''
        available = db.execute('SELECT COUNT(*) FROM (' + query + ')', arguments).fetchone()[0]
        selected = db.execute(query + ' ORDER BY record_key LIMIT ?', [*arguments, maximum]).fetchall()
        chunks, total = [], 0
        for row in selected:
            source = dict(row)
            payload = json.loads(source.pop('payload_json'))
            source.pop('n')
            source['coverage'] = json.loads(source.pop('coverage_json'))
            source['title'] = payload.get('title', '') if isinstance(payload.get('title', ''), str) else ''
            for field in (*TEXT_FIELDS[1:], 'title'):
                value = payload.get(field)
                if not isinstance(value, str) or not value.strip():
                    continue
                count = max(1, math.ceil(max(0, len(value) - size) / (size - overlap)) + 1)
                total += count
                for offset in range(min(count, max_chunks - len(chunks))):
                    start = offset * (size - overlap)
                    end = min(len(value), start + size)
                    text = value[start:end]
                    chunks.append({**source, 'excerpt': text, 'excerpt_field': field,
                        'excerpt_range': {'start': start, 'end': end, 'unit': 'unicode_code_points', 'end_exclusive': True},
                        'text_sha256': _digest(text),
                        'reference': f"record:{row['record_key']}@{row['content_hash']}"})
    return chunks, {'available_records': available,
        'selected_records': len(selected), 'available_chunks_in_selected_records': total,
        'selected_chunks': len(chunks), 'max_records': maximum, 'max_chunks': max_chunks,
        'record_limit_reached': len(selected) < available, 'chunk_limit_reached': len(chunks) < total,
        'source_job_id': source_job, 'version_policy': 'latest_observed_in_selected_job' if source_job else 'latest_observed',
        'fields': list(TEXT_FIELDS), 'chunk_chars': size, 'overlap_chars': overlap}


def _identity(root):
    manifest_text = (root / 'runtime/semantic-model-manifest.json').read_text(encoding='utf-8')
    manifest = json.loads(manifest_text)
    implementation = Path(__file__).resolve().parents[1] / 'web/semantic_embeddings.py'
    identity = _digest(canonical_json({'manifest_sha256': _digest(manifest_text),
        'implementation_sha256': hashlib.sha256(implementation.read_bytes()).hexdigest(),
        'files': manifest['files'], 'pooling': manifest['pooling'], 'max_tokens': manifest['max_tokens'],
        'query_batch_size': 1, 'document_batch_size': 1}))
    return identity, manifest


def _cache_key(identity, chunk):
    return _digest(canonical_json([identity, chunk['record_key'], chunk['content_hash'],
        chunk['excerpt_field'], chunk['excerpt_range'], chunk['text_sha256']]))


def _cache(path, keys, dimensions, writes=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL)')
        if writes is not None:
            for key, value in writes.items():
                payload = canonical_json(value)
                db.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)', (key, payload, _digest(payload)))
            return
        found = {}
        for key in keys:
            row = db.execute('SELECT payload,sha256 FROM vectors WHERE key=?', (key,)).fetchone()
            if row is None or _digest(row[0]) != row[1]:
                continue
            try:
                value = json.loads(row[0])
                vector = value['vector']
                if len(vector) != dimensions or not all(type(x) in (int, float) and math.isfinite(x) for x in vector):
                    continue
                if not isinstance(value['coverage'], dict):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            found[key] = value
        return found


async def _settled(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        finally:
            raise


async def retrieve_semantic(context, params=None):
    params = context.params if params is None else params
    query, limit = _parameters(params.get('query'), params.get('limit', 10))
    chunks, index = await _settled(_corpus, context.store, params)
    await context.check_control()
    identity, manifest = await _settled(_identity, context.paths.root)
    keys = [_cache_key(identity, item) for item in chunks]
    cache_path = context.paths.root / 'cache/semantic-vectors.sqlite3'
    cached = await _settled(_cache, cache_path, keys, manifest['dimensions'])
    missing = [i for i, key in enumerate(keys) if key not in cached]
    # Release the CPU lease immediately after the worker, including embedded
    # answering calls which keep the parent job resources alive afterwards.
    async with AsyncExitStack() as resources:
        local_context = copy.copy(context)
        local_context.resources = resources
        await lease(local_context, 'cpu-models', 'semantic')
        encoded = await controlled(context, SemanticRuntime(context.paths.root).arun(
            {'operation': 'embed', 'query': query, 'batch_size': 1,
             'texts': [chunks[i]['excerpt'] for i in missing]}, timeout=120))
    if len(encoded['embeddings']) != len(missing) or len(encoded['input_coverage']) != len(missing):
        raise ValueError('Semantic worker returned inconsistent batch sizes')
    # Runtime independently verifies every actual model asset. Check that the
    # identity used for cache lookup still describes this very worker invocation.
    if (await _settled(_identity, context.paths.root))[0] != identity:
        raise ValueError('Semantic model identity changed during retrieval')
    query_vector, query_coverage = encoded['query_embedding'], encoded['query_coverage']
    writes = {}
    for offset, index_value in enumerate(missing):
        item = {'vector': encoded['embeddings'][offset], 'coverage': encoded['input_coverage'][offset]}
        if item['coverage']['text_sha256'] != chunks[index_value]['text_sha256']:
            raise ValueError('Semantic embedding source hash mismatch')
        writes[keys[index_value]] = item
    await _settled(_cache, cache_path, [], manifest['dimensions'], writes)
    cached.update(writes)
    ranked, truncated = [], 0
    for key, chunk in zip(keys, chunks):
        item = cached[key]
        coverage = item['coverage']
        truncated += bool(coverage['truncated'])
        span = coverage['encoded_character_range']
        start = chunk['excerpt_range']['start']
        ranked.append({**chunk, 'score': sum(a*b for a, b in zip(query_vector, item['vector'], strict=True)),
            'retrieval_method': 'local_semantic_cosine', 'embedding_coverage': coverage,
            'encoded_source_range': {**span, 'start': start + span['start'], 'end': start + span['end']}})
    ranked.sort(key=lambda item: (-item['score'], item['record_key'], item['excerpt_field'], item['excerpt_range']['start']))
    reasons = [key for key in ('record_limit_reached', 'chunk_limit_reached') if index[key]]
    if truncated:
        reasons.append('document_embedding_token_truncation')
    if query_coverage['truncated']:
        reasons.append('query_embedding_token_truncation')
    return {'status': 'partial' if reasons else 'complete', 'mode': 'local_semantic_search', 'query': query,
        'model_used': True, 'network_used': False, 'gpu_used': False, 'results': ranked[:limit],
        'returned': min(limit, len(ranked)), 'limit': limit, 'index': index, 'coverage_complete': not reasons,
        'coverage_limits': reasons, 'embedding': encoded['embedding'], 'query_coverage': query_coverage,
        'truncated_chunks': truncated, 'cache': {'hits': len(keys) - len(missing), 'misses': len(missing),
            'identity': identity, 'query_encoded': True, 'document_batch_size': 1, 'query_batch_size': 1}, 'metrics': encoded.get('metrics', {}),
        'score_interpretation': 'Cosine similarity ranks the selected local chunks; it is not an accuracy probability.'}


async def search_semantic(context):
    result = await retrieve_semantic(context)
    record = Record('local_search', 'semantic_result', context.job_id, '', result,
        coverage={'scope': 'bounded_local_semantic_index', 'generated': True,
                  'complete': result['coverage_complete'], 'limits': result['coverage_limits']})
    checkpoint = await context.checkpoint('semantic_search')
    await context.commit_page('semantic_search', [record], next_cursor=None, exhausted=True,
        expected_revision=checkpoint['revision'] if checkpoint else 0, metadata={'result': result})
    return result
