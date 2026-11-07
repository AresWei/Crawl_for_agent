# Source-derived Crawl4AI EmbeddingStrategy, commit838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Apache-2.0 and attribution retained; see adaptive-source-manifest.json.
# Changes: injected local embedding acquisition; no global remote provider imports.
from __future__ import annotations
import numpy as np

def cosine_similarity(a,b):
    return float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)))

def cosine_distance(a,b):
    return 1-cosine_similarity(a,b)

class EmbeddingStrategy:

    def __init__(self, embedding_model: str=None, llm_config: Union[LLMConfig, Dict]=None, query_llm_config: Union[LLMConfig, Dict]=None):
        self.embedding_model = embedding_model or 'sentence-transformers/all-MiniLM-L6-v2'
        self.llm_config = llm_config
        self.query_llm_config = query_llm_config
        self._embedding_cache = {}
        self._link_embedding_cache = {}
        self._validation_passed = False
        self._distance_matrix_cache = None
        self._kb_embeddings_hash = None
        self._validation_embeddings_cache = None
        self._kb_similarity_threshold = 0.95

    def _compute_distance_matrix(self, query_embeddings: Any, kb_embeddings: Any) -> Any:
        """Compute distance matrix using vectorized operations"""
        if kb_embeddings is None or len(kb_embeddings) == 0:
            return None
        if len(query_embeddings.shape) == 1:
            query_embeddings = query_embeddings.reshape(1, -1)
        if len(kb_embeddings.shape) == 1:
            kb_embeddings = kb_embeddings.reshape(1, -1)
        query_norm = query_embeddings / np.linalg.norm(query_embeddings, axis=1, keepdims=True)
        kb_norm = kb_embeddings / np.linalg.norm(kb_embeddings, axis=1, keepdims=True)
        similarity_matrix = np.dot(query_norm, kb_norm.T)
        distance_matrix = 1 - similarity_matrix
        return distance_matrix

    def _get_cached_distance_matrix(self, query_embeddings: Any, kb_embeddings: Any) -> Any:
        """Get distance matrix with caching"""
        if kb_embeddings is None or len(kb_embeddings) == 0:
            return None
        kb_hash = hash(kb_embeddings.tobytes()) if kb_embeddings is not None else None
        if self._distance_matrix_cache is None or kb_hash != self._kb_embeddings_hash:
            self._distance_matrix_cache = self._compute_distance_matrix(query_embeddings, kb_embeddings)
            self._kb_embeddings_hash = kb_hash
        return self._distance_matrix_cache

    def compute_coverage_shape(self, query_points: Any, alpha: float=0.5):
        """Find the minimal shape that covers all query points using alpha shape"""
        try:
            if len(query_points) < 3:
                return None
            query_points = np.array(query_points)
            coverage = {'center': np.mean(query_points, axis=0), 'std': np.std(query_points, axis=0), 'points': query_points, 'radius': np.max(np.linalg.norm(query_points - np.mean(query_points, axis=0), axis=1))}
            return coverage
        except Exception:
            return None

    def _sample_boundary_points(self, shape, n_samples: int=20) -> List[Any]:
        """Sample points from the boundary of a shape"""
        if shape is None:
            return []
        return []

    def find_coverage_gaps(self, kb_embeddings: Any, query_embeddings: Any) -> List[Tuple[Any, float]]:
        """Calculate gap distances for all query variations using vectorized operations"""
        gaps = []
        if kb_embeddings is None or len(kb_embeddings) == 0:
            for q_emb in query_embeddings:
                gaps.append((q_emb, 1.0))
            return gaps
        distance_matrix = self._get_cached_distance_matrix(query_embeddings, kb_embeddings)
        if distance_matrix is None:
            for q_emb in query_embeddings:
                gaps.append((q_emb, 1.0))
            return gaps
        min_distances = np.min(distance_matrix, axis=1)
        for i, q_emb in enumerate(query_embeddings):
            gaps.append((q_emb, min_distances[i]))
        return gaps

    async def select_links_for_expansion(self, candidate_links: List[Link], gaps: List[Tuple[Any, float]], kb_embeddings: Any) -> List[Tuple[Link, float]]:
        """Select links that most efficiently fill the gaps"""
        import hashlib
        scored_links = []
        links_to_embed = []
        texts_to_embed = []
        link_embeddings_map = {}
        for link in candidate_links:
            link_text = ' '.join(filter(None, [link.text or '', link.title or '', link.meta.get('description', '') if hasattr(link, 'meta') and link.meta else '', link.head_data.get('meta', {}).get('description', '') if link.head_data else '']))
            if not link_text.strip():
                continue
            cache_key = hashlib.md5(f'{link.href}:{link_text}'.encode()).hexdigest()
            if cache_key in self._link_embedding_cache:
                link_embeddings_map[link.href] = self._link_embedding_cache[cache_key]
            else:
                links_to_embed.append(link)
                texts_to_embed.append(link_text)
        if texts_to_embed:
            new_embeddings = await self._get_embeddings(texts_to_embed)
            for link, text, embedding in zip(links_to_embed, texts_to_embed, new_embeddings):
                cache_key = hashlib.md5(f'{link.href}:{text}'.encode()).hexdigest()
                self._link_embedding_cache[cache_key] = embedding
                link_embeddings_map[link.href] = embedding
        coverage_radius = self.config.embedding_coverage_radius if hasattr(self, 'config') else 0.2
        for link in candidate_links:
            if link.href not in link_embeddings_map:
                continue
            link_embedding = link_embeddings_map[link.href]
            if not gaps:
                score = 0.0
            else:
                gaps_helped = 0
                total_improvement = 0
                for gap_point, gap_distance in gaps:
                    if gap_distance > coverage_radius:
                        new_distance = cosine_distance(link_embedding, gap_point)
                        if new_distance < gap_distance:
                            improvement = gap_distance - new_distance
                            scaled_improvement = improvement * 2
                            total_improvement += scaled_improvement
                            gaps_helped += 1
                gaps_needing_help = sum((1 for _, d in gaps if d > coverage_radius))
                if gaps_needing_help > 0:
                    gap_reduction_score = total_improvement / gaps_needing_help
                else:
                    gap_reduction_score = 0
                if kb_embeddings is not None and len(kb_embeddings) > 0:
                    link_norm = link_embedding / np.linalg.norm(link_embedding)
                    kb_norm = kb_embeddings / np.linalg.norm(kb_embeddings, axis=1, keepdims=True)
                    similarities = np.dot(kb_norm, link_norm)
                    max_similarity = np.max(similarities)
                    overlap_threshold = self.config.embedding_overlap_threshold if hasattr(self, 'config') else 0.85
                    if max_similarity > overlap_threshold:
                        overlap_penalty = (max_similarity - overlap_threshold) * 2
                    else:
                        overlap_penalty = 0
                else:
                    overlap_penalty = 0
                score = gap_reduction_score * (1 - overlap_penalty)
                if hasattr(link, 'contextual_score') and link.contextual_score:
                    score = score * 0.8 + link.contextual_score * 0.2
            scored_links.append((link, score))
        return sorted(scored_links, key=lambda x: x[1], reverse=True)

    async def calculate_confidence(self, state: CrawlState) -> float:
        """Coverage-based learning score (0–1)."""
        if state.kb_embeddings is None or state.query_embeddings is None:
            return 0.0
        if len(state.kb_embeddings) == 0 or len(state.query_embeddings) == 0:
            return 0.0
        Q = np.asarray(state.query_embeddings, dtype=np.float32)
        D = np.asarray(state.kb_embeddings, dtype=np.float32)
        Q /= np.linalg.norm(Q, axis=1, keepdims=True) + 1e-08
        D /= np.linalg.norm(D, axis=1, keepdims=True) + 1e-08
        best = (Q @ D.T).max(axis=1)
        tau = getattr(self.config, 'coverage_tau', None)
        score = float((best >= tau).mean()) if tau is not None else float(best.mean())
        state.metrics['coverage_score'] = score
        state.metrics['avg_best_similarity'] = float(best.mean())
        state.metrics['median_best_similarity'] = float(np.median(best))
        return score

    async def rank_links(self, state: CrawlState, config: AdaptiveConfig) -> List[Tuple[Link, float]]:
        """Main entry point for link ranking"""
        self.config = config
        seen_urls = set()
        uncrawled_links = []
        for link in state.pending_links:
            if link.href not in state.crawled_urls and link.href not in seen_urls:
                uncrawled_links.append(link)
                seen_urls.add(link.href)
        if not uncrawled_links:
            return []
        gaps = self.find_coverage_gaps(state.kb_embeddings, state.query_embeddings)
        state.semantic_gaps = [(g[0].tolist(), g[1]) for g in gaps]
        return await self.select_links_for_expansion(uncrawled_links, gaps, state.kb_embeddings)

    async def validate_coverage(self, state: CrawlState) -> float:
        """Validate coverage using held-out queries with caching"""
        if not hasattr(self, '_validation_queries') or not self._validation_queries:
            return state.metrics.get('confidence', 0.0)
        if self._validation_embeddings_cache is None:
            self._validation_embeddings_cache = await self._get_embeddings(self._validation_queries)
        val_embeddings = self._validation_embeddings_cache
        if state.kb_embeddings is None or len(state.kb_embeddings) == 0:
            return 0.0
        distance_matrix = self._compute_distance_matrix(val_embeddings, state.kb_embeddings)
        if distance_matrix is None:
            return 0.0
        min_distances = np.min(distance_matrix, axis=1)
        scores = 1.0 - min_distances
        validation_confidence = np.mean(scores)
        state.metrics['validation_confidence'] = validation_confidence
        return validation_confidence

    async def should_stop(self, state: CrawlState, config: AdaptiveConfig) -> bool:
        """Stop based on learning curve convergence"""
        confidence = state.metrics.get('confidence', 0.0)
        min_confidence_threshold = config.embedding_min_confidence_threshold if hasattr(config, 'embedding_min_confidence_threshold') else 0.1
        if confidence < min_confidence_threshold and len(state.crawled_urls) > 0:
            state.metrics['stopped_reason'] = 'below_minimum_relevance_threshold'
            state.metrics['is_irrelevant'] = True
            return True
        if len(state.crawled_urls) >= config.max_pages or not state.pending_links:
            return True
        if not hasattr(state, 'confidence_history'):
            state.confidence_history = []
        state.confidence_history.append(confidence)
        if len(state.confidence_history) < 2:
            return False
        improvement_diffs = list(zip(state.confidence_history[:-1], state.confidence_history[1:]))
        avg_improvement = sum((abs(b - a) for a, b in improvement_diffs)) / len(improvement_diffs)
        state.metrics['avg_improvement'] = avg_improvement
        min_relative_improvement = self.config.embedding_min_relative_improvement * confidence if hasattr(self, 'config') else 0.1 * confidence
        if avg_improvement < min_relative_improvement:
            val_score = await self.validate_coverage(state)
            validation_min = self.config.embedding_validation_min_score if hasattr(self, 'config') else 0.4
            if val_score > validation_min:
                state.metrics['stopped_reason'] = 'converged_validated'
                self._validation_passed = True
                return True
            else:
                state.metrics['stopped_reason'] = 'low_validation'
        return False

    async def update_state(self, state: CrawlState, new_results: List[CrawlResult]) -> None:
        """Update embeddings and coverage metrics with deduplication"""
        new_texts = []
        valid_results = []
        for result in new_results:
            content = result.markdown.raw_markdown if hasattr(result, 'markdown') and result.markdown else ''
            if content:
                new_texts.append(content[:5000])
                valid_results.append(result)
        if not new_texts:
            return
        new_embeddings = await self._get_embeddings(new_texts)
        if state.kb_embeddings is None:
            state.kb_embeddings = new_embeddings
            deduplicated_indices = list(range(len(new_embeddings)))
        else:
            deduplicated_embeddings = []
            deduplicated_indices = []
            for i, new_emb in enumerate(new_embeddings):
                new_emb_normalized = new_emb / np.linalg.norm(new_emb)
                kb_normalized = state.kb_embeddings / np.linalg.norm(state.kb_embeddings, axis=1, keepdims=True)
                similarities = np.dot(kb_normalized, new_emb_normalized)
                if np.max(similarities) < self._kb_similarity_threshold:
                    deduplicated_embeddings.append(new_emb)
                    deduplicated_indices.append(i)
            if deduplicated_embeddings:
                state.kb_embeddings = np.vstack([state.kb_embeddings, np.array(deduplicated_embeddings)])
        for idx in deduplicated_indices:
            state.crawl_order.append(valid_results[idx].url)
        self._kb_embeddings_hash = None
        self._distance_matrix_cache = None
        if hasattr(state, 'query_embeddings') and state.query_embeddings is not None:
            state.coverage_shape = self.compute_coverage_shape(state.query_embeddings, self.config.alpha_shape_alpha if hasattr(self, 'config') else 0.5)
