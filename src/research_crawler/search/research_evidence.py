"""Add bounded multilingual passages without discarding lexical source evidence."""
import asyncio
import hashlib
import json

from .evidence_semantic import rerank_evidence


VERSION = 'lexical-plus-semantic-2026-11-07-v1'


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def enrich_evidence(context, sources, *, question, query, ranker=rerank_evidence):
    originals = sources['evidence']
    mode = context.params.get('evidence_selection', 'lexical')
    if mode not in {'lexical', 'hybrid'}:
        raise ValueError('evidence_selection must be lexical or hybrid')
    if mode == 'lexical' or not originals:
        return {'evidence': originals, 'selection': {'mode': 'lexical', 'model_used': False}}
    def fallback(reason):
        return {'evidence': originals, 'selection': {'mode': mode, 'status': 'partial',
                'reason': reason, 'used': 'lexical', 'semantic_support_proven': False}}
    if len(question) > 1000:
        return fallback('semantic_question_limit')
    # Ranking reads only the exact collected versions, never the current URL or
    # an unrelated newer version in the store. Model/config changes invalidate it.
    paths = [context.paths.root/'runtime/semantic-model-manifest.json',
             context.paths.root/'src/research_crawler/search/evidence_semantic.py',
             context.paths.root/'scripts/semantic-worker.py']
    try:
        identities = [await asyncio.to_thread(path.read_bytes) for path in paths]
    except FileNotFoundError:
        return fallback('semantic_runtime_unavailable')
    key = _sha({'version': VERSION, 'question': question, 'query': query,
                'evidence': originals,
                'implementation': [hashlib.sha256(data).hexdigest() for data in identities]})
    checkpoint = await context.checkpoint('discovery_evidence')
    if checkpoint and checkpoint['metadata'].get('input_key') == key and checkpoint['exhausted']:
        return checkpoint['metadata']['summary']
    records = await asyncio.to_thread(context.store.observations, context.job_id, limit=1000)
    versions = {(r['record_key'], r['content_hash']): r for r in records
                if r['source'] == 'web' and r['kind'] == 'page'}
    pages, matching = [], []
    for item in originals:
        row = versions.get((item['record_key'], item['content_hash']))
        if row:
            pages.append({**row, 'url': item['url']})
            matching.append(item)
    if not pages:
        return {'evidence': originals, 'selection': {'mode': mode, 'status': 'partial',
                'reason': 'exact_collected_versions_unavailable', 'model_used': False}}
    try:
        ranked = await ranker(context, pages, question=question, query=query, per_page=2)
    except (RuntimeError, TimeoutError):
        return fallback('semantic_worker_failed_or_timed_out')
    output = [dict(item) for item in originals]
    remaining = max(0, 7500-sum(len(item['excerpt']) for item in output))
    added = 0
    for page in ranked['pages']:
        original = matching[page['page_index']]
        row = pages[page['page_index']]
        for hit in page['hits']:
            field, span = hit['excerpt_field'], hit['excerpt_range']
            text = row['payload'][field]
            if text[span['start']:span['end']] != hit['excerpt']:
                raise ValueError('Ranked evidence no longer matches its original version')
            old = original['excerpt_range']
            overlap = max(0, min(span['end'], old['end'])-max(span['start'], old['start'])) if field == original['excerpt_field'] else 0
            # Repeated evidence adds tokens rather than coverage. Keep the first
            # novel ranked passage per source; never truncate a quotation to fit.
            if overlap or not hit['excerpt'].strip() or len(hit['excerpt']) > remaining:
                continue
            output.append({**original, 'citation': len(output)+1,
                'excerpt': hit['excerpt'], 'excerpt_field': field, 'excerpt_range': span,
                'coverage': {**original.get('coverage', {}), 'answer_uses': 'exact_bounded_excerpt'},
                'selection': {'method': VERSION, 'semantic_score': hit['score'],
                              'semantic_support_proven': False,
                              'embedding_coverage': hit.get('embedding_coverage'),
                              'encoded_source_ranges': hit.get('encoded_source_ranges')}})
            remaining -= len(hit['excerpt'])
            added += 1
            break
        if len(output) >= 6:
            break
    summary = {'evidence': output, 'selection': {'mode': mode, 'version': VERSION,
               'model_used': ranked['model_used'], 'additional_passages': added,
               'original_passages_retained': len(originals), 'ranking': ranked}}
    await context.commit_page('discovery_evidence', [], next_cursor=None, exhausted=True,
        expected_revision=checkpoint['revision'] if checkpoint else 0,
        metadata={'input_key': key, 'summary': summary})
    return summary
