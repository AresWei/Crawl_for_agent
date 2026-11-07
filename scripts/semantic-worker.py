"""One bounded request per worker. Model state leaves RAM when the worker exits."""
from pathlib import Path
import json
import sys
import time
import threading
import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

def main(request):
    operation = request.get('operation')
    options = request.get('options', {})
    if not isinstance(options, dict):
        raise ValueError('options must be an object')
    result = {'operation':operation,'network_used':False,'gpu_used':False}
    if operation in {'nlp_sentence','nlp_topic'}:
        from research_crawler.web.nlp_chunking import NlpSentenceChunking, TopicSegmentationChunking, source_ranges
        text = request.get('text', '')
        if not isinstance(text,str) or len(text) > 1_000_000:
            raise ValueError('NLP text budget exceeded')
        chunker = (NlpSentenceChunking if operation == 'nlp_sentence' else TopicSegmentationChunking)(data_path=ROOT/'cache/nltk', **options)
        chunks = chunker.chunk(text)
        result.update(chunks=chunks, source_ranges=source_ranges(text,chunks), model_used=False,
            algorithm='NLTK Punkt' if operation == 'nlp_sentence' else 'NLTK TextTiling',
            keywords=[chunker.extract_keywords(chunk) for chunk in chunks] if operation == 'nlp_topic' else None)
        return result
    if operation not in {'embed','evidence_embed','cluster','rank','topics'}:
        raise ValueError('Unknown semantic operation')
    texts = request.get('texts', [])
    max_texts = 1024 if operation == 'evidence_embed' else 256
    if not isinstance(texts,list) or len(texts)>max_texts or any(not isinstance(t,str) or len(t)>32000 for t in texts):
        raise ValueError(f'Expected at most {max_texts} texts of at most 32000 characters')
    if sum(map(len,texts)) > 1_000_000:
        raise ValueError('Semantic input character budget exceeded')
    if operation == 'topics':
        from research_crawler.web.semantic_topics import LocalTopicClassifier
        classifier = LocalTopicClassifier(ROOT)
        result.update(model_used=True,topics=classifier(texts),topic_model=classifier.metadata)
        return result
    from research_crawler.web.semantic_embeddings import OnnxEmbeddings
    embedder = OnnxEmbeddings(ROOT)
    result.update(model_used=True)
    if operation in {'embed','evidence_embed'}:
        batch_size = request.get('batch_size',8)
        if type(batch_size) is not int or not 1<=batch_size<=8:
            raise ValueError('batch_size must be an integer from 1 to 8')
        if operation == 'evidence_embed':
            queries = request.get('queries', [])
            if (not isinstance(queries,list) or not 1<=len(queries)<=8 or
                any(not isinstance(q,str) or not q.strip() or len(q)>1000 for q in queries)):
                raise ValueError('Expected 1..8 nonempty evidence queries, at most 1000 characters each')
            if sum(map(len,texts))+sum(map(len,queries))>1_000_000:
                raise ValueError('Semantic input character budget exceeded')
            result['query_embeddings'] = embedder.encode(queries,batch_size=1).tolist()
            result['query_coverages'] = embedder.last_input_coverage
        elif 'query' in request:
            query = request['query']
            if not isinstance(query,str) or not query.strip() or len(query)>1000:
                raise ValueError('Expected nonempty query, at most 1000 characters')
            result['query_embedding'] = embedder.encode([query],batch_size=1)[0].tolist()
            result['query_coverage'] = embedder.last_input_coverage[0]
        result['embeddings'] = embedder.encode(texts,batch_size=batch_size).tolist()
        result['input_coverage'] = embedder.last_input_coverage
        result['batching'] = {'query_batch_size':1 if 'query' in request or operation=='evidence_embed' else None,'document_batch_size':batch_size}
    elif operation == 'rank':
        import numpy as np
        query = request.get('query','')
        if not isinstance(query,str) or not query.strip() or len(query)>1000:
            raise ValueError('Expected nonempty query, at most 1000 characters')
        vectors = embedder.encode([query]+texts)
        scores = vectors[1:] @ vectors[0]
        result['ranking'] = [{'index':int(i), 'score':float(scores[i])} for i in np.argsort(-scores, kind='stable')]
        result['query_coverage'] = embedder.last_input_coverage[0]
        result['input_coverage'] = embedder.last_input_coverage[1:]
    elif operation == 'cluster':
        from research_crawler.web.semantic_processing import CosineStrategy
        classifier = None
        if request.get('topic_labels'):
            from research_crawler.web.semantic_topics import LocalTopicClassifier
            classifier = LocalTopicClassifier(ROOT)
            result['topic_model'] = classifier.metadata
        allowed = {'semantic_filter','word_count_threshold','max_dist','linkage_method','top_k','sim_threshold'}
        if set(options)-allowed:
            raise ValueError('Unknown cosine options')
        strategy = CosineStrategy(embedder=embedder, classifier=classifier, **options)
        result['clusters'] = strategy.run(request.get('url',''), texts)
        result['embedding_inference_coverage'] = embedder.input_coverage
    result['embedding'] = embedder.metadata()
    return result

if __name__ == '__main__':
    import psutil
    process = psutil.Process()
    peak = [process.memory_info().rss]
    finished = threading.Event()
    def sample():
        while not finished.wait(0.05):
            peak[0] = max(peak[0], process.memory_info().rss)
    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    start = time.perf_counter()
    cpu_start = time.process_time()
    try:
        result = main(json.load(sys.stdin))
        result['metrics'] = {'worker_wall_ms':round((time.perf_counter()-start)*1000,3),
            'worker_cpu_ms':round((time.process_time()-cpu_start)*1000,3),
            'sampled_peak_rss_bytes':max(peak[0], process.memory_info().rss),'rss_sampling_interval_ms':50,
            'threads':2,'provider':('nltk CPU' if result['operation'].startswith('nlp_') else
                'torch CPU' if result['operation']=='topics' else
                'onnxruntime CPUExecutionProvider + torch CPU' if 'topic_model' in result else 'onnxruntime CPUExecutionProvider'),
            'gpu_used':False}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        finished.set()
