"""One bounded CPU pass ranks exact spans of already collected original text.

Similarity selects candidates; it never certifies claim entailment or translates
source text. Expanded context is explicitly distinct from the model-encoded span.
"""
from __future__ import annotations
import asyncio
from bisect import bisect_left
from contextlib import AsyncExitStack
import copy
import hashlib
import json
import math
import re
from ..core.exclusive import lease
from ..core.sessions import controlled
from ..web.semantic_runtime import SemanticRuntime

PROJECTION_VERSION='markdown-visible-source-map-v1'


def _ranges(positions):
    result=[]
    for position in positions:
        if result and result[-1]['end']==position:result[-1]['end']=position+1
        else:result.append(_span(position,position+1))
    return result


def _markdown_projection(text):
    """Omit Markdown link syntax/destinations, retaining original label offsets.

    Parse the entire field before windowing: a window starting halfway through a
    URL cannot expose its tail as visible text. Fenced and inline code stay code.
    """
    keep=bytearray(b'\1')*len(text);protected=bytearray(len(text));fence=None;offset=0
    for line in text.splitlines(keepends=True):
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        if marker:
            token=marker.group(1)
            if fence is None:fence=token
            elif token[0]==fence[0] and len(token)>=len(fence):fence=None
            protected[offset:offset+len(line)]=b'\1'*len(line)
        elif fence:protected[offset:offset+len(line)]=b'\1'*len(line)
        elif re.match(r'^\s{0,3}\[[^\]\n]+\]:\s*\S+',line):keep[offset:offset+len(line)]=b'\0'*len(line)
        offset+=len(line)
    for match in re.finditer(r'(`+)([^\n]*?)\1',text):
        protected[match.start():match.end()]=b'\1'*(match.end()-match.start())
    pattern=re.compile(r'!?\[((?:\\.|[^\]\\\n]|\[[^\]\n]*\])*)\]\(')
    for match in pattern.finditer(text):
        if protected[match.start()] or not keep[match.start()]:continue
        cursor=match.end();depth=1;quote=None
        while cursor<len(text) and cursor-match.end()<16384:
            char=text[cursor]
            if char=='\\':cursor+=2;continue
            if quote:
                if char==quote:quote=None
            elif char in {'"',"'"} and cursor>match.end() and text[cursor-1].isspace():quote=char
            elif char=='(':depth+=1
            elif char==')':
                depth-=1
                if not depth:break
            elif char=='\n':break
            cursor+=1
        if depth:continue
        label_start,label_end=match.span(1)
        keep[match.start():label_start]=b'\0'*(label_start-match.start())
        keep[label_end:cursor+1]=b'\0'*(cursor+1-label_end)
    for match in re.finditer(r'!?\[([^\]\n]+)\]\[[^\]\n]*\]',text):
        if protected[match.start()] or not keep[match.start()]:continue
        left,right=match.span(1)
        keep[match.start():left]=b'\0'*(left-match.start())
        keep[right:match.end()]=b'\0'*(match.end()-right)
    positions=[i for i,value in enumerate(keep) if value]
    visible=''.join(text[i] for i in positions)
    return {'text':visible,'positions':positions,'version':PROJECTION_VERSION,
            'sha256':_sha(visible),'omitted_source_ranges':_ranges(i for i,value in enumerate(keep) if not value)}


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _span(start, end):
    return {'start':start,'end':end,'unit':'unicode_code_points','end_exclusive':True}


def _integer(value, low, high, name):
    if type(value) is not int or not low<=value<=high:
        raise ValueError(f'{name} must be an integer {low}..{high}')


def _probes(question, query):
    for name, value in [('question',question),('query',query)]:
        if not isinstance(value,str) or len(value)>1000 or (name=='question' and not value.strip()):
            raise ValueError(f'{name} must be text of at most 1000 characters')
    probes=[{'text':question,'origin':'question','range':_span(0,len(question))}]
    if query.strip() and query!=question:
        probes.append({'text':query,'origin':'query','range':_span(0,len(query))})
    # Independent probes are actual interrogative clauses, not trailing format
    # instructions. This uses punctuation, not language/domain keyword lists.
    for sentence in re.finditer(r'[^。！？!?;；]*[?？]',question):
        for match in re.finditer(r'[^，,?？]+',sentence.group()):
            text=match.group(); start=sentence.start()+match.start()
            left=len(text)-len(text.lstrip()); text=text.strip();start+=left
            if len(text)>=8 and text not in {p['text'] for p in probes}:
                probes.append({'text':text,'origin':'question_clause','range':_span(start,start+len(text))})
            if len(probes)==8:return probes
    return probes


def _score(scores,probes):
    primary=[score for score,probe in zip(scores,probes,strict=True) if probe['origin']!='question_clause']
    secondary=[score for score,probe in zip(scores,probes,strict=True) if probe['origin']=='question_clause']
    anchor=sum(primary)/len(primary)
    return .8*anchor+.2*max(secondary) if secondary else anchor


def _even(items, maximum):
    if len(items)<=maximum:return items
    if maximum==1:return [items[len(items)//2]]
    return [items[(i*(len(items)-1))//(maximum-1)] for i in range(maximum)]


def _prepare(pages, max_chunks, max_scan_chars):
    if not isinstance(pages,list) or len(pages)>32:
        raise ValueError('Expected at most 32 already collected pages')
    prepared=[]
    for index,page in enumerate(pages):
        if not isinstance(page,dict) or not isinstance(page.get('payload'),dict) or not isinstance(page.get('url'),str):
            raise ValueError('Each page requires url and payload')
        payload=page['payload'];reason=None
        if (payload.get('source_usable') is False or payload.get('blocking',{}).get('exclude_from_answers') or
            payload.get('http_error_response') or (type(payload.get('status_code')) is int and payload['status_code']>=400)):
            reason='source_excluded'
        field=next((k for k in ('markdown','text','content','fit_markdown') if isinstance(payload.get(k),str) and payload[k].strip()),None)
        text=payload[field] if field else ''
        if not field:reason=reason or 'no_original_text'
        if page.get('source') in {'local_model','local_search','analysis','comment_analysis','derived_exports','local_creation','web_search'}:
            reason='derived_source_excluded'
        identity={key:page[key] for key in ('record_key','content_hash','job_id','source_job_id','observed_at','source','kind','native_id','source_date','source_date_field','coverage') if key in page}
        identity.update(url=page['url'],field=field,field_sha256=_sha(text),field_characters=len(text),
                        source_sha256=payload.get('source_sha256'))
        projection=_markdown_projection(text) if field in {'markdown','fit_markdown'} and not reason else {
            'text':text,'positions':list(range(len(text))),'version':'identity-v1','sha256':_sha(text),'omitted_source_ranges':[]}
        prepared.append({'page_index':index,'url':page['url'],'identity':identity,'field':field,'text':text,'projection':projection,
                         'reason':reason,'candidates':[],'scan_ranges':[]})
    active=[p for p in prepared if not p['reason']]
    # Fair document allocation; no first-page starvation. Oversized corpora are
    # explicitly sampled across the full field rather than silently prefix-cut.
    remaining=max_scan_chars
    for n,page in enumerate(sorted(active,key=lambda p:len(p['text']))):
        allowance=min(len(page['text']),remaining//(len(active)-n));remaining-=allowance
        text=page['text']
        starts=list(range(0,len(text),360))
        if allowance<len(text):
            starts=_even(starts,max(1,allowance//420)) if allowance>=40 else []
        for start in starts:
            end=min(start+420,len(text));part=text[start:end]
            projection=page['projection'];mapping=projection['positions']
            begin=bisect_left(mapping,start);stop=bisect_left(mapping,end)
            projected=projection['text'][begin:stop];positions=mapping[begin:stop]
            page['scan_ranges'].append(_span(start,end))
            if len(projected.strip())>=40:
                page['candidates'].append({'start':start,'end':end,'text':part,'embedding_text':projected,
                    'positions':positions,'page_index':page['page_index']})
        page['scan_complete']=allowance==len(text)
    available=sum(len(p['candidates']) for p in prepared)
    remaining=max_chunks; selected=[]
    for n,page in enumerate(sorted(active,key=lambda p:len(p['candidates']))):
        count=min(len(page['candidates']),remaining//(len(active)-n));remaining-=count
        candidates=_even(page['candidates'],count) if count else []
        page['selected_count']=len(candidates);selected.extend(candidates)
    selected.sort(key=lambda p:(p['page_index'],p['start']))
    return prepared,selected,available


def _validate_encoding(text, vector, coverage, dimensions):
    if not isinstance(vector,list) or len(vector)!=dimensions or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector):
        raise ValueError('Invalid evidence embedding vector')
    if not isinstance(coverage,dict) or coverage.get('text_sha256')!=_sha(text) or coverage.get('input_characters')!=len(text):
        raise ValueError('Evidence embedding source identity mismatch')
    span=coverage.get('encoded_character_range',{})
    if (type(span.get('start')) is not int or type(span.get('end')) is not int or
        not 0<=span['start']<=span['end']<=len(text) or span.get('unit')!='unicode_code_points' or span.get('end_exclusive') is not True):
        raise ValueError('Invalid evidence embedding character range')


def _context_range(text,start,end,maximum=900):
    # Expand only to nearby paragraph boundaries, and keep the ranked chunk in
    # full. No expansion is represented as semantically encoded.
    before=text.rfind('\n\n',max(0,start-240),start)
    left=before+2 if before>=0 else start
    after=text.find('\n\n',end,min(len(text),end+240))
    right=after if after>=0 else end
    if right-left>maximum:left=start;right=end
    return left,right


def select_evidence_candidates(pages,probes,per_page=2):
    """Pure offline selection: preserve one topic anchor per source, then cover
    the least represented original question facets without averaging them away.
    Returns original candidate objects and exact context bounds; no inference.
    """
    chosen={p['page_index']:[] for p in pages}
    facets=[i for i,p in enumerate(probes) if p['origin']=='question_clause']
    all_candidates=[c for p in pages for c in p['candidates']]
    def available(page,candidate):
        left,right=_context_range(page['text'],candidate['start'],candidate['end'])
        if all(right<=entry[1] or left>=entry[2] for entry in chosen[page['page_index']]):
            return left,right
        return None
    for page in pages:
        if page['candidates']:
            best=min(page['candidates'],key=lambda c:(-c['score'],c['start']))
            left,right=_context_range(page['text'],best['start'],best['end'])
            chosen[page['page_index']].append((best,left,right,None))
    served=set()
    while any(len(chosen[p['page_index']])<per_page for p in pages):
        options=[(p,c,available(p,c)) for p in pages if len(chosen[p['page_index']])<per_page
                 for c in p['candidates'] if available(p,c) is not None]
        if not options:break
        pending=[f for f in facets if f not in served]
        if pending:
            def covered(f):
                ceiling=max((max(0,c['probe_scores'][f]) for c in all_candidates),default=0)
                existing=max((max(0,e[0]['probe_scores'][f]) for entries in chosen.values() for e in entries),default=0)
                return existing/ceiling if ceiling else 1
            facet=min(pending,key=lambda f:(covered(f),f));served.add(facet)
            page,candidate,bounds=max(options,key=lambda v:(v[1]['probe_scores'][facet],v[1]['score'],-v[0]['page_index'],-v[1]['start']))
        else:
            facet=None
            page,candidate,bounds=max(options,key=lambda v:(v[1]['score'],-v[0]['page_index'],-v[1]['start']))
        chosen[page['page_index']].append((candidate,*bounds,facet))
    return chosen


async def rerank_evidence(context,pages,*,question,query='',per_page=2,max_chunks=1024,max_scan_chars=600000,timeout=120):
    _integer(per_page,1,2,'per_page');_integer(max_chunks,1,1024,'max_chunks')
    _integer(max_scan_chars,420,900000,'max_scan_chars')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 1<=timeout<=180:
        raise ValueError('timeout must be finite in 1..180 seconds')
    probes=_probes(question,query)
    prepared,chunks,available=await asyncio.to_thread(_prepare,pages,max_chunks,max_scan_chars)
    await context.check_control()
    base={'mode':'collected_evidence_semantic_rerank','candidate_status':'experimental_not_quality_approved',
          'model_used':False,'network_used':False,'gpu_used':False,
          'semantic_support_proven':False,'score_interpretation':'Cosine relevance of selected encoded spans, not entailment or probability.',
          'pages':[],'coverage_limits':[],'index':{'available_chunks':available,'selected_chunks':len(chunks),
          'max_chunks':max_chunks,'max_scan_chars':max_scan_chars,'chunk_chars':420,'overlap_chars':60},'probes':probes}
    if not chunks:
        base.update(status='partial',coverage_complete=False,coverage_limits=['no_eligible_chunks'],
                    pages=[{'url':p['url'],'page_index':p['page_index'],'source_identity':p['identity'],'hits':[],'reason':p['reason'] or 'no_candidates'} for p in prepared])
        return base
    manifest_path=context.paths.root/'runtime/semantic-model-manifest.json'
    manifest_text=await asyncio.to_thread(manifest_path.read_text,encoding='utf-8');manifest=json.loads(manifest_text)
    async with AsyncExitStack() as resources:
        local=copy.copy(context);local.resources=resources
        await lease(local,'cpu-models','semantic')
        encoded=await controlled(context,SemanticRuntime(context.paths.root).arun(
            {'operation':'evidence_embed','queries':[p['text'] for p in probes],
             'texts':[c['embedding_text'] for c in chunks],'batch_size':8},timeout=timeout))
    if _sha(await asyncio.to_thread(manifest_path.read_text,encoding='utf-8'))!=_sha(manifest_text):
        raise ValueError('Semantic model identity changed during evidence ranking')
    vectors=encoded['embeddings'];coverages=encoded['input_coverage'];queries=encoded['query_embeddings'];qcoverages=encoded['query_coverages']
    if len(vectors)!=len(chunks) or len(coverages)!=len(chunks) or len(queries)!=len(probes) or len(qcoverages)!=len(probes):
        raise ValueError('Evidence worker returned inconsistent batch sizes')
    for probe,vector,coverage in zip(probes,queries,qcoverages,strict=True):
        _validate_encoding(probe['text'],vector,coverage,manifest['dimensions']);probe['embedding_coverage']=coverage
    for chunk,vector,coverage in zip(chunks,vectors,coverages,strict=True):
        _validate_encoding(chunk['embedding_text'],vector,coverage,manifest['dimensions'])
        scores=[sum(a*b for a,b in zip(vector,q,strict=True)) for q in queries]
        chunk.update(vector=vector,coverage=coverage,score=_score(scores,probes),probe_index=scores.index(max(scores)),probe_scores=scores)
    reasons=[]
    if available>len(chunks):reasons.append('candidate_limit_reached')
    if any(not p.get('scan_complete',True) for p in prepared):reasons.append('source_scan_limit_reached')
    if any(c.get('truncated') for c in coverages):reasons.append('document_embedding_token_truncation')
    if any(c.get('truncated') for c in qcoverages):reasons.append('query_embedding_token_truncation')
    selection=select_evidence_candidates([{**p,'candidates':[c for c in chunks if c['page_index']==p['page_index']]} for p in prepared],probes,per_page)
    for page in prepared:
        hits=[]
        for selected,left,right,selection_probe in selection[page['page_index']]:
            source=selected['coverage']['encoded_character_range'];excerpt=page['text'][left:right]
            encoded_positions=selected['positions'][source['start']:source['end']]
            encoded_ranges=_ranges(encoded_positions)
            hits.append({'excerpt':excerpt,'excerpt_field':page['field'],'excerpt_range':_span(left,right),
                'text_sha256':_sha(excerpt),'score':selected['score'],'probe_index':selected['probe_index'],
                'probe_scores':selected['probe_scores'],'selection_probe_index':selection_probe,
                'retrieval_method':'multilingual_cpu_anchor_then_question_coverage',
                'semantic_support_proven':False,'ranked_source_range':_span(selected['start'],selected['end']),
                'embedding_coverage':selected['coverage'],
                'embedding_input_projection':{'version':page['projection']['version'],'text_sha256':_sha(selected['embedding_text']),
                    'text':selected['embedding_text'],'source_ranges':_ranges(selected['positions'])},
                'encoded_source_ranges':encoded_ranges,
                'encoded_source_range':_span(encoded_positions[0],encoded_positions[-1]+1) if encoded_positions else _span(selected['start'],selected['start']),
                'encoded_source_range_is_bounding_only':True,
                'context_expansion_encoded':False})
        base['pages'].append({'url':page['url'],'page_index':page['page_index'],'source_identity':page['identity'],
            'hits':hits,'reason':page['reason'],'candidate_count':len(page['candidates']),
            'selected_count':page.get('selected_count',0),'scan_complete':page.get('scan_complete',False),
            'sampled_source_ranges':page['scan_ranges'] if not page.get('scan_complete',True) else [],
            'projection':{k:v for k,v in page['projection'].items() if k not in {'text','positions'}},
            'candidate_diagnostics':[{'ranked_source_range':_span(c['start'],c['end']),
                'score':c['score'],'probe_scores':c['probe_scores'],'embedding_coverage':c['coverage'],
                'projection_sha256':_sha(c['embedding_text']),
                'encoded_source_ranges':_ranges(c['positions'][c['coverage']['encoded_character_range']['start']:c['coverage']['encoded_character_range']['end']])}
                for c in chunks if c['page_index']==page['page_index']],
            'encoded_candidate_ranges':[_span(c['start'],c['end']) for c in chunks if c['page_index']==page['page_index']]})
    base.update(status='partial' if reasons else 'complete',coverage_complete=not reasons,coverage_limits=reasons,
                model_used=True,embedding=encoded['embedding'],manifest_sha256=_sha(manifest_text),metrics=encoded.get('metrics',{}),
                worker_calls=1,ranking={'primary_weight':.8,'primary_aggregation':'mean_of_full_question_and_query_if_present',
                    'interrogative_clause_weight':.2,'interrogative_clause_aggregation':'max',
                    'selection':'one_primary_per_page_then_least_represented_question_facet',
                    'no_hard_query_and_filter':True})
    return base
