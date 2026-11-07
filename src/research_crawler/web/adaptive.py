"""Adaptive query-guided selection and stopping inside the atomic web frontier.

Scores are source-derived heuristics, never factual confidence or full-site coverage.
The caller commits dump() alongside the captured page and pending frontier.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import httpx
from types import SimpleNamespace

from .adaptive_algorithms import StatisticalStrategy


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _integer(config, key, default, low, high):
    value=config.get(key,default)
    if type(value) is not int or not low<=value<=high:
        raise ValueError(f'adaptive.{key} must be an integer in {low}..{high}')
    return value


def _number(config,key,default,low=0,high=1):
    value=config.get(key,default)
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError(f'adaptive.{key} must be finite in {low}..{high}')
    return float(value)


class _CachedStatisticalStrategy(StatisticalStrategy):
    """Keep original scoring identical while avoiding repeated full-page tokenization."""
    def __init__(self):
        super().__init__()
        self.document_terms={}

    def _get_document_terms(self,result):
        if result.url not in self.document_terms:
            self.document_terms[result.url]=super()._get_document_terms(result)
        return self.document_terms[result.url]


class AdaptiveController:
    """One query's bounded source corpus; construction never accesses network/models."""
    VERSION=1
    KEYS={'query','strategy','confidence_threshold','saturation_threshold','min_gain_threshold',
          'relevance_weight','novelty_weight','authority_weight','max_pages','min_pages','top_k_links',
          'max_document_chars','max_corpus_chars','max_terms','max_link_chars','max_links',
          'query_expansion','n_query_variations','embedding_coverage_radius','embedding_overlap_threshold',
          'embedding_min_confidence_threshold','embedding_min_relative_improvement','embedding_validation_min_score',
          'max_embedding_texts','max_embedding_calls','embedding_timeout','coverage_tau'}

    def __init__(self,config,snapshot=None,*,context=None,embedding_runner=None,model=None):
        if not isinstance(config,dict) or set(config)-self.KEYS:
            raise ValueError('adaptive must contain supported explicit configuration fields')
        query=config.get('query')
        if not isinstance(query,str) or not query.strip() or len(query)>2000:
            raise ValueError('adaptive.query must be nonempty and at most 2000 characters')
        mode=config.get('strategy','statistical')
        if mode not in {'statistical','embedding'}:
            raise ValueError('adaptive.strategy must be statistical or embedding')
        embedding_fields={'query_expansion','n_query_variations','embedding_coverage_radius','embedding_overlap_threshold',
            'embedding_min_confidence_threshold','embedding_min_relative_improvement','embedding_validation_min_score',
            'max_embedding_texts','max_embedding_calls','embedding_timeout','coverage_tau'}
        if mode!='embedding' and set(config)&embedding_fields:
            raise ValueError('Embedding parameters require adaptive.strategy=embedding')
        cfg={key:_number(config,key,default) for key,default in {
            'confidence_threshold':.7,'saturation_threshold':.8,'min_gain_threshold':.1,
            'relevance_weight':.5,'novelty_weight':.3,'authority_weight':.2}.items()}
        if abs(sum(cfg[k] for k in ('relevance_weight','novelty_weight','authority_weight'))-1)>.001:
            raise ValueError('adaptive link weights must sum to one')
        cfg.update({key:_integer(config,key,*limits) for key,limits in {
            'max_pages':(20,1,128),'min_pages':(1,1,128),'top_k_links':(3,1,32),
            'max_document_chars':(100000,100,500000),'max_corpus_chars':(1000000,100,4000000),
            'max_terms':(20000,10,100000),'max_link_chars':(4000,100,16000),
            'max_links':(1000,1,10000)}.items()})
        if cfg['min_pages']>cfg['max_pages']:raise ValueError('adaptive.min_pages exceeds max_pages')
        cfg.update(query=query.strip(),strategy=mode)
        if mode=='embedding':
            if type(config.get('query_expansion',False)) is not bool:raise ValueError('query_expansion must be boolean')
            cfg['query_expansion']=config.get('query_expansion',False)
            cfg['alpha_shape_alpha']=.5  # Frozen upstream centroid/radius implementation ignores this value.
            cfg.update({key:_number(config,key,default) for key,default in {
                'embedding_coverage_radius':.2,'embedding_overlap_threshold':.85,
                'embedding_min_confidence_threshold':.1,'embedding_min_relative_improvement':.1,
                'embedding_validation_min_score':.4}.items()})
            cfg.update({key:_integer(config,key,*limits) for key,limits in {
                'n_query_variations':(6,4,20),'max_embedding_texts':(256,1,512),
                'max_embedding_calls':(64,1,256),'embedding_timeout':(120,1,300)}.items()})
            cfg['coverage_tau']=None if config.get('coverage_tau') is None else _number(config,'coverage_tau',.7)
        self.config=SimpleNamespace(**cfg)
        self.config_hash=_hash(json.dumps(cfg,sort_keys=True,separators=(',',':')))
        self.strategy=_CachedStatisticalStrategy()
        self.state=SimpleNamespace(query=cfg['query'],crawled_urls=set(),knowledge_base=[],pending_links=[],metrics={},
            term_frequencies=defaultdict(int),document_frequencies=defaultdict(int),documents_with_terms=defaultdict(set),
            total_documents=0,new_terms_history=[],crawl_order=[])
        self.documents=[]
        self.link_evidence={}
        self.limits=[]
        self.skipped=[]
        self.last_reason=None
        self.embedding=None
        if mode=='embedding':
            from .adaptive_embedding import EmbeddingAdaptive
            self.embedding=EmbeddingAdaptive(self.config,context,embedding_runner,model)
        if snapshot:self._restore(snapshot)

    def _restore(self,snapshot):
        if not isinstance(snapshot,dict) or snapshot.get('version')!=self.VERSION or snapshot.get('config_hash')!=self.config_hash:
            raise ValueError('Adaptive checkpoint version/config does not match')
        documents=snapshot.get('documents',[])
        if not isinstance(documents,list) or len(documents)>self.config.max_pages:
            raise ValueError('Invalid adaptive checkpoint corpus')
        if sum(len(d.get('markdown','')) for d in documents)>self.config.max_corpus_chars:
            raise ValueError('Adaptive checkpoint exceeds corpus budget')
        for document in documents:
            text=document['markdown'];url=document['url']
            if len(text)>self.config.max_document_chars or document['markdown_sha256']!=_hash(text) or url in self.state.crawled_urls:
                raise ValueError('Adaptive checkpoint source is invalid or duplicated')
            self.documents.append(dict(document))
            self.state.knowledge_base.append(SimpleNamespace(url=url,markdown=SimpleNamespace(raw_markdown=text)))
            self.state.crawled_urls.add(url)
        data=snapshot.get('statistics',{})
        for key in ('term_frequencies','document_frequencies'):
            values=data.get(key,{})
            if len(values)>self.config.max_terms or any(type(v) is not int or v<0 for v in values.values()):
                raise ValueError('Invalid adaptive checkpoint statistics')
            setattr(self.state,key,defaultdict(int,values))
        self.state.documents_with_terms=defaultdict(set,{k:set(v) for k,v in data.get('documents_with_terms',{}).items()})
        self.state.total_documents=len(documents)
        self.state.crawl_order=[d['url'] for d in documents]
        self.state.new_terms_history=list(data.get('new_terms_history',[]))
        # Validate derived counters against bound source bytes. Replayed/corrupted
        # statistics must not silently raise coverage or alter stopping decisions.
        tf=Counter();df=Counter();members=defaultdict(set);history=[]
        for index,document in enumerate(documents):
            terms=self.strategy._get_document_terms(self.state.knowledge_base[index])
            old=len(tf);tf.update(terms);history.append(len(tf)-old)
            for term in set(terms):df[term]+=1;members[term].add(index)
        if (dict(tf)!=dict(self.state.term_frequencies) or dict(df)!=dict(self.state.document_frequencies)
            or dict(members)!=dict(self.state.documents_with_terms) or history!=self.state.new_terms_history):
            raise ValueError('Adaptive checkpoint statistics do not match source corpus')
        self.state.metrics=dict(snapshot.get('metrics',{}))
        if documents:
            expected={'coverage':self.strategy._calculate_coverage(self.state),
                'consistency':self.strategy._calculate_consistency(self.state),
                'saturation':self.strategy._calculate_saturation(self.state)}
            expected['confidence']=.4*expected['coverage']+.3*expected['consistency']+.3*expected['saturation']
            if any(self.state.metrics.get(key)!=value for key,value in expected.items()):
                raise ValueError('Adaptive checkpoint metrics do not match source corpus')
        self.link_evidence=dict(snapshot.get('links',{}))
        if len(self.link_evidence)>self.config.max_links:raise ValueError('Adaptive checkpoint links exceed budget')
        self.limits=list(snapshot.get('limits',[]));self.skipped=list(snapshot.get('skipped',[]))
        self.last_reason=snapshot.get('last_reason')
        if self.embedding and snapshot.get('embedding') is not None:
            self.embedding.restore(snapshot['embedding'],self.documents)

    def _limit(self,reason):
        if reason not in self.limits:self.limits.append(reason)

    async def observe(self,url,payload,source_complete=True):
        if url in self.state.crawled_urls:return
        if not source_complete:
            if url not in self.skipped:self.skipped.append(url)
            return
        text=payload.get('markdown','')
        if not isinstance(text,str):raise ValueError('Adaptive source Markdown must be text')
        if not text.strip():
            if url not in self.skipped:self.skipped.append(url)
            return
        if len(self.documents)>=self.config.max_pages:
            self._limit('adaptive_page_budget');return
        if len(text)>self.config.max_document_chars:
            self._limit('adaptive_document_budget');return
        if sum(len(d['markdown']) for d in self.documents)+len(text)>self.config.max_corpus_chars:
            self._limit('adaptive_corpus_budget');return
        terms=set(self.strategy._tokenize(text.lower()))
        if len(terms|set(self.state.term_frequencies))>self.config.max_terms:
            self._limit('adaptive_term_budget');return
        result=SimpleNamespace(url=url,markdown=SimpleNamespace(raw_markdown=text))
        self.strategy.document_terms[url]=self.strategy._tokenize(text.lower())
        self.state.knowledge_base.append(result)
        self.state.crawled_urls.add(url)
        await self.strategy.update_state(self.state,[result])
        self.documents.append({'url':url,'final_url':payload.get('final_url',url),
            'title':payload.get('title',''),'markdown':text,'markdown_sha256':_hash(text),
            'source_fetched_at':payload.get('source_fetched_at'),'source_field':'markdown',
            'source_range':[0,len(text)],'html_sha256':_hash(payload.get('html',''))})
        if url in self.skipped:self.skipped.remove(url)
        for link in payload.get('links',[]):
            href=link.get('url',link.get('href'))
            if not href or href in self.link_evidence:continue
            if len(self.link_evidence)>=self.config.max_links:
                self._limit('adaptive_link_metadata_budget');break
            values={key:link.get(key,'') for key in ('text','title')}
            if any(not isinstance(value,str) for value in values.values()):continue
            truncated=any(len(value)>self.config.max_link_chars for value in values.values())
            values={key:value[:self.config.max_link_chars] for key,value in values.items()}
            values.update(href=href,source_url=url,head_data={},contextual_score=None,intrinsic_score=None,truncated=truncated)
            self.link_evidence[href]=values
            if truncated:self._limit('adaptive_link_text_budget')
        self.state.metrics['confidence']=await self.strategy.calculate_confidence(self.state)
        if self.embedding:
            await self._embedding_call(self.embedding.observe(self.documents))

    async def _embedding_call(self,operation):
        try:
            return await operation
        except (ValueError,RuntimeError,OSError,TimeoutError,ImportError,KeyError,TypeError,httpx.HTTPError) as error:
            self.embedding.last_error=type(error).__name__
            self.embedding.limit('adaptive_embedding_unavailable_or_invalid')

    async def rank_pending(self,pending):
        # Candidates originate exclusively in the core's depth/domain/filter policy.
        if self.embedding:
            if not self.embedding.limits:
                await self._embedding_call(self.embedding.rank(pending,self.link_evidence))
            return pending
        self.state.pending_links=[SimpleNamespace(**{k:v for k,v in self.link_evidence.get(item['url'],{
            'href':item['url'],'text':'','title':'','head_data':{},'contextual_score':None,'intrinsic_score':None}).items()
            if k not in {'source_url','truncated'}}) for item in pending]
        ranked=await self.strategy.rank_links(self.state,self.config)
        scores={link.href:score for link,score in ranked}
        for item in pending:item['adaptive_score']=scores.get(item['url'],0.0)
        pending.sort(key=lambda item:(-item['adaptive_score'],item.get('depth',0),item['url']))
        return pending

    async def decision(self,pending):
        await self.rank_pending(pending)
        self.last_reason=None
        if self.limits:self.last_reason=self.limits[0]
        elif len(self.documents)>=self.config.max_pages:self.last_reason='adaptive_page_budget'
        elif self.embedding:
            if self.embedding.limits:self.last_reason=self.embedding.limits[0]
            elif not self.skipped:self.last_reason=await self._embedding_call(self.embedding.decision(pending))
            if self.embedding.limits:self.last_reason=self.embedding.limits[0]
        elif len(self.documents)>=self.config.min_pages and not self.skipped:
            if self.state.metrics.get('confidence',0)>=self.config.confidence_threshold:self.last_reason='adaptive_confidence'
            elif self.state.metrics.get('saturation',0)>=self.config.saturation_threshold:self.last_reason='adaptive_saturation'
            elif pending and pending[0].get('adaptive_score',0)<self.config.min_gain_threshold:self.last_reason='adaptive_gain'
        # Empty core frontier is naturally exhausted by core; do not manufacture a limit.
        if not pending and not self.limits and not (self.embedding and self.embedding.limits):self.last_reason=None
        return self.last_reason

    def dump(self):
        return {'version':self.VERSION,'config_hash':self.config_hash,'documents':[dict(d) for d in self.documents],
            'statistics':{'term_frequencies':dict(self.state.term_frequencies),'document_frequencies':dict(self.state.document_frequencies),
                'documents_with_terms':{key:sorted(value) for key,value in self.state.documents_with_terms.items()},
                'new_terms_history':list(self.state.new_terms_history)},'metrics':dict(self.state.metrics),
            'links':dict(self.link_evidence),'limits':list(self.limits),'skipped':list(self.skipped),'last_reason':self.last_reason,
            **({'embedding':self.embedding.dump()} if self.embedding else {})}

    def summary(self):
        return {'strategy':self.config.strategy,'query':self.config.query,'documents':len(self.documents),
            'metrics':self.embedding.summary()['metrics'] if self.embedding else dict(self.state.metrics),'confidence_is_probability':False,'full_site_coverage':False,
            'reason':self.last_reason,'limits':list(self.limits),'excluded_incomplete_or_empty':len(self.skipped),
            'knowledge_base':[{'url':d['url'],'markdown_sha256':d['markdown_sha256'],'source_field':'markdown','source_range':d['source_range']} for d in self.documents],
            'tokenization':'upstream Unicode word split, tokens longer than two; Chinese segmentation not added',
            'confidence_weights':{'coverage':.4,'consistency':.3,'saturation':.3},
            'authority':'upstream active statistical rank uses constant one; authority helper is not invoked',
            **({'embedding':self.embedding.summary()} if self.embedding else {})}

    def relevant_content(self,top_k=5):
        if type(top_k) is not int or not 1<=top_k<=128:raise ValueError('top_k must be in 1..128')
        query_terms=set(self.config.query.lower().split())
        result=[]
        for index,document in enumerate(self.documents):
            score=len(query_terms&set(document['markdown'].lower().split()))/len(query_terms) if query_terms else 0.0
            result.append(dict(document,score=score,index=index))
        return sorted(result,key=lambda item:-item['score'])[:top_k]
