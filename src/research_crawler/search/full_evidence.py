"""Expand current-job source excerpts without search, inference or version changes."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json

from ..core.records import canonical_json
from ..core.sessions import controlled

VERSION='current-job-full-field-context-v1'
STREAM='full_evidence'
DEFAULT_CHARS=96000
MAX_CHARS=120000


def _sha(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _span(start,end):
    return {'start':start,'end':end,'unit':'unicode_code_points','end_exclusive':True}


def _load_exact(context,evidence):
    documents=[];citations=set()
    with context.store.connection() as db:
        for item in evidence:
            if not isinstance(item,dict) or type(item.get('citation')) is not int or item['citation']<1 or item['citation'] in citations:
                raise ValueError('Evidence requires distinct positive citation IDs')
            citations.add(item['citation'])
            key,version=item.get('record_key'),item.get('content_hash')
            if not isinstance(key,str) or not isinstance(version,str):
                raise ValueError('Evidence requires an exact record key and version')
            row=db.execute('''SELECT v.payload_json,v.source_date,v.source_date_field,o.url
                FROM observations o JOIN records r ON r.key=o.record_key
                JOIN record_versions v ON v.record_key=o.record_key AND v.content_hash=o.content_hash
                WHERE o.job_id=? AND o.record_key=? AND o.content_hash=? AND r.source='web' AND r.kind='page' ''',
                (context.job_id,key,version)).fetchone()
            if row is None:raise ValueError('Exact web/page version is not observed in this job')
            payload=json.loads(row['payload_json'])
            if _sha({'payload':payload,'source_date':row['source_date'],'source_date_field':row['source_date_field']})!=version:
                raise ValueError('Stored source content disagrees with its version hash')
            if item.get('reference')!=f'record:{key}@{version}' or item.get('url')!=row['url']:
                raise ValueError('Evidence reference or URL disagrees with its exact observation')
            field=item.get('excerpt_field');text=payload.get(field) if isinstance(field,str) else None
            span=item.get('excerpt_range')
            if (not isinstance(text,str) or not isinstance(span,dict) or
                type(span.get('start')) is not int or type(span.get('end')) is not int or
                span.get('unit')!='unicode_code_points' or span.get('end_exclusive') is not True or
                not 0<=span['start']<span['end']<=len(text) or text[span['start']:span['end']]!=item.get('excerpt')):
                raise ValueError('Original excerpt must match its exact source field and range')
            documents.append({'original':copy.deepcopy(item),'text':text,'start':span['start'],'end':span['end']})
    return documents


def _expand(documents,budget):
    # First reserve every original excerpt. No budget permits silently deleting
    # a previously supplied source or its selected passage.
    allocations=[d['end']-d['start'] for d in documents]
    minimum=sum(allocations)
    if minimum>budget:raise ValueError('source_context_chars is smaller than the original excerpts')
    remaining=budget-minimum
    for index in sorted(range(len(documents)),key=lambda i:(len(documents[i]['text']),i)):
        needed=len(documents[index]['text'])-allocations[index]
        if needed<=remaining:
            allocations[index]+=needed;remaining-=needed
    partial=[i for i,d in enumerate(documents) if allocations[i]<len(d['text'])]
    for position,index in enumerate(partial):
        extra=min(len(documents[index]['text'])-allocations[index],remaining//(len(partial)-position))
        allocations[index]+=extra;remaining-=extra
    evidence=[]
    for document,width in zip(documents,allocations,strict=True):
        text=document['text'];original=document['original'];extra=width-(document['end']-document['start'])
        start=max(0,document['start']-extra//2);end=min(len(text),start+width);start=max(0,end-width)
        complete=start==0 and end==len(text)
        item={**original,'excerpt':text[start:end],'excerpt_range':_span(start,end),
              'original_excerpt_range':copy.deepcopy(original['excerpt_range']),
              'field_characters':len(text),'complete_field':complete,
              'original_coverage':copy.deepcopy(original.get('coverage',{})),
              'coverage':{**original.get('coverage',{}),'answer_uses':'exact_source_field_context',
                          'scope':'provided_source_field_context','complete':complete,
                          'complete_field':complete,'context_partial':not complete}}
        evidence.append(item)
    return evidence


async def enrich_full_evidence(context,sources):
    """Return evidence plus explicit field coverage, within one shared task.

    Full-field coverage is not proof that the original HTTP page was exhaustive.
    Checkpoints reuse only identical source versions, input ranges and config.
    """
    if not isinstance(sources,dict) or not isinstance(sources.get('evidence'),list):
        raise ValueError('sources must contain an evidence list')
    evidence=sources['evidence']
    if len(evidence)>5:raise ValueError('At most five original sources may be expanded')
    budget=context.params.get('source_context_chars',DEFAULT_CHARS)
    if type(budget) is not int or not 1<=budget<=MAX_CHARS:
        raise ValueError('source_context_chars must be an integer 1..120000')
    binding={'version':VERSION,'source_context_chars':budget,'input_evidence_sha256':_sha(evidence)}
    binding_hash=_sha(binding)
    checkpoint=await context.checkpoint(STREAM)
    # Even a cache hit must still refer to exact current-job source observations.
    documents=await controlled(context,asyncio.to_thread(_load_exact,context,evidence))
    if checkpoint and checkpoint['metadata'].get('binding_sha256')==binding_hash:
        saved=copy.deepcopy(checkpoint['metadata']['summary'])
        saved['selection']['cache_reused']=True
        return saved
    expanded=_expand(documents,budget)
    complete=bool(expanded) and all(item['complete_field'] for item in expanded)
    result={'evidence':expanded,'selection':{
        'method':VERSION,'status':'complete' if complete else 'partial','coverage_complete':complete,
        'reason':None if complete else 'no_source_evidence' if not expanded else 'source_context_budget','source_count':len(expanded),
        'source_context_chars':budget,'selected_characters':sum(len(e['excerpt']) for e in expanded),
        'total_field_characters':sum(len(d['text']) for d in documents),
        'original_excerpt_characters':sum(d['end']-d['start'] for d in documents),
        'binding_sha256':binding_hash,'cache_reused':False,'network_used':False,'model_used':False,
        'coverage_scope':'provided_original_source_fields','full_web_page_proven':False}}
    await context.commit_page(STREAM,[],next_cursor=None,exhausted=True,
        expected_revision=checkpoint['revision'] if checkpoint else 0,
        metadata={'binding':binding,'binding_sha256':binding_hash,'summary':copy.deepcopy(result)})
    return result
