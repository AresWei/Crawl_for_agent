"""Local semantic processing over captured source, with explicit input ranges."""
import copy
import hashlib
import re
from contextlib import AsyncExitStack

from ..core.exclusive import lease
from ..core.sessions import controlled
from .semantic_runtime import SemanticRuntime


async def worker(context,request):
    # Keep the shared CPU slot for inference only, not for the rest of a crawl
    # or an enclosing local-answer task. All semantic entry points share it.
    async with AsyncExitStack() as resources:
        scope=copy.copy(context);scope.resources=resources
        await lease(scope,'cpu-models','semantic')
        return await controlled(context,SemanticRuntime(context.paths.root).arun(request,timeout=180))


def locate_sections(text,chunks):
    result=[];cursor=0
    for chunk in chunks:
        start=text.find(chunk,cursor)
        normalized=False
        end=start+len(chunk)
        if start<0:
            words=chunk.split()
            match=re.search(r'\s+'.join(re.escape(word) for word in words),text[cursor:]) if words else None
            if match:
                start=cursor+match.start();end=cursor+match.end();normalized=True
        if start<0:
            result.append({'text':chunk,'source_range':None,'source_alignment':'unresolved'})
            continue
        cursor=start+1
        result.append({'text':chunk,'source_range':{'start':start,'end':end,'unit':'unicode_code_points','end_exclusive':True},
                       'source_excerpt':text[start:end],
                       'source_alignment':'whitespace_normalized' if normalized else 'exact'})
    return result


async def enrich(context,payload):
    text=payload.get('markdown','')
    chunking=context.params.get('chunking',{})
    operation=chunking.get('type')
    if operation in {'nlp_sentence','nlp_topic'}:
        try:
            result=await worker(context,{'operation':operation,'text':text,
                'options':{key:value for key,value in chunking.items() if key!='type'}})
            payload['chunks']=result['chunks']
            payload['chunk_source_ranges']=result['source_ranges']
            payload['nlp_chunking']={**result,'status':'complete','source_field':'markdown',
                'source_text_sha256':hashlib.sha256(text.encode()).hexdigest()}
        except (RuntimeError,ValueError,TimeoutError):
            payload['nlp_chunking']={'status':'partial','reason':'local_nlp_processing_failed','source_field':'markdown'}
    config=context.params.get('semantic_extraction')
    if config is None:return payload
    if not isinstance(config,dict):raise ValueError('semantic_extraction must be an object')
    allowed={'semantic_filter','word_count_threshold','max_dist','linkage_method','top_k','sim_threshold','topic_labels','max_sections'}
    if set(config)-allowed:raise ValueError('Unknown semantic extraction option')
    maximum=config.get('max_sections',128)
    if type(maximum) is not int or not 1<=maximum<=256:raise ValueError('max_sections must be1..256')
    if type(config.get('topic_labels',False)) is not bool:raise ValueError('topic_labels must be boolean')
    sections=locate_sections(text,payload.get('chunks',[]))
    selected=sections[:maximum]
    texts=[item['text'] for item in selected]
    # Do not silently shorten a source section. The worker enforces its own
    # input character/token bounds and reports actual encoded ranges.
    try:
        result=await worker(context,{'operation':'cluster','texts':texts,'url':payload.get('final_url',''),
            'topic_labels':config.get('topic_labels',False),
            'options':{key:value for key,value in config.items() if key not in {'topic_labels','max_sections'}}})
        incomplete=len(sections)>maximum or any(item['source_range'] is None for item in selected)
        incomplete=incomplete or result.get('embedding',{}).get('truncated_inputs',0)>0
        incomplete=incomplete or any(cluster.get('topic_coverage',{}).get('truncated',False) for cluster in result['clusters'])
        for cluster in result['clusters']:
            cluster['source_evidence']=[{'section_index':index,'field':'markdown',
                **selected[index]} for index in cluster['section_indices']]
        payload['semantic_extraction']={**result,'status':'partial' if incomplete else 'complete','model_claim':True,
            'source_field':'markdown','source_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'coverage':{'complete':not incomplete,'available_sections':len(sections),'processed_sections':len(selected),
                'omitted_sections':max(0,len(sections)-maximum),'scope':'selected chunks of captured markdown',
                'semantic_accuracy_verified':False}}
    except (RuntimeError,ValueError,TimeoutError):
        payload['semantic_extraction']={'status':'partial','model_claim':True,'clusters':[],
            'coverage':{'complete':False,'reason':'local_semantic_processing_failed'}}
    return payload
