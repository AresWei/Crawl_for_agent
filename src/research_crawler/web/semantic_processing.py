# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and semantic-source-manifest.json.
# Changes: local injected CPU models, bounded input, singleton fix, masked pooling,
# original section references, and actual topic probabilities instead of hidden inference.
from __future__ import annotations
from typing import Any


class CosineStrategy:
    DEL = '<|DEL|>'

    def __init__(self, semantic_filter=None, word_count_threshold=10, max_dist=0.2,
                 linkage_method='ward', top_k=3, sim_threshold=0.3, *, embedder,
                 classifier=None, max_sections=256, **kwargs):
        self.semantic_filter = semantic_filter
        self.word_count_threshold = word_count_threshold
        self.max_dist = max_dist
        self.linkage_method = linkage_method
        self.top_k = top_k
        self.sim_threshold = sim_threshold
        self.embedder = embedder
        self.classifier = classifier
        self.max_sections = max_sections
        self.input_format = kwargs.get('input_format', 'markdown')
        if linkage_method not in {'single','complete','average','weighted','centroid','median','ward'}:
            raise ValueError('Unsupported linkage_method')
        if not 0 <= max_dist <= 2 or not -1 <= sim_threshold <= 1:
            raise ValueError('Invalid cosine threshold')

    def get_embeddings(self, sentences, batch_size=None, bypass_buffer=False):
        return self.embedder.encode(sentences, batch_size=batch_size or 8)

    def _filter_indices(self, documents, semantic_filter, at_least_k=20):
        if not semantic_filter:
            return list(range(len(documents)))
        if len(documents) < at_least_k:
            at_least_k = max(1, len(documents) // 2)
        query = self.get_embeddings([semantic_filter])[0]
        vectors = self.get_embeddings(documents)
        similarities = vectors @ query
        selected = [i for i, score in enumerate(similarities) if score >= self.sim_threshold]
        remaining = sorted((i for i, score in enumerate(similarities) if score < self.sim_threshold),
                           key=lambda i: (-similarities[i], i))
        selected.extend(remaining[:max(0, at_least_k-len(selected))])
        return selected[:at_least_k]

    def filter_documents_embeddings(self, documents, semantic_filter, at_least_k=20):
        return [documents[i] for i in self._filter_indices(documents, semantic_filter, at_least_k)]

    def hierarchical_clustering(self, sentences, embeddings=None):
        import numpy as np
        from scipy.cluster.hierarchy import linkage, fcluster
        from scipy.spatial.distance import pdist
        if len(sentences) <= 1:
            return np.ones(len(sentences), dtype=int)
        vectors = self.get_embeddings(sentences) if embeddings is None else embeddings
        distances = np.maximum(pdist(vectors, 'cosine'), 0)
        return fcluster(linkage(distances, method=self.linkage_method), self.max_dist, criterion='distance')

    def filter_clusters_by_word_count(self, clusters):
        return {key: texts for key, texts in clusters.items()
                if len(' '.join(texts).split()) >= self.word_count_threshold}

    def run(self, url: str, sections: list[str], *q, **kwargs) -> list[dict[str, Any]]:
        if len(sections) > self.max_sections:
            raise ValueError(f'Semantic section budget exceeded ({self.max_sections})')
        indices = self._filter_indices(sections, self.semantic_filter)
        if not indices:
            return []
        selected = [sections[i] for i in indices]
        labels = self.hierarchical_clustering(selected)
        clusters = {}
        members = {}
        for position, label in enumerate(labels):
            label = int(label)
            clusters.setdefault(label, []).append(selected[position])
            members.setdefault(label, []).append(indices[position])
        filtered = self.filter_clusters_by_word_count(clusters)
        result = [{'index':index, 'content':' '.join(filtered[index]), 'tags':[],
            'section_indices':members[index], 'topic_status':'not_requested' if self.classifier is None else 'model_inference',
            'model_claim':True, 'semantic_accuracy_automatically_proven':False} for index in sorted(filtered)]
        if self.classifier is not None and result:
            predictions = self.classifier([item['content'] for item in result])
            for item, prediction in zip(result, predictions, strict=True):
                item['tags'] = prediction['labels']
                item['topic_scores'] = prediction['scores']
                item['topic_coverage'] = {key:prediction[key] for key in
                    ('model','revision','language','label_count','threshold','max_tokens','truncated') if key in prediction}
        return result

    def extract(self, url: str, html: str, *q, **kwargs):
        sections = html.split(self.DEL)
        if len(sections) == 1:
            sections = [part.strip() for part in html.split('\n\n') if part.strip()]
        return self.run(url, sections, **kwargs)

    async def arun(self, url, sections, *q, **kwargs):
        import asyncio
        return await asyncio.to_thread(self.run, url, sections, *q, **kwargs)
