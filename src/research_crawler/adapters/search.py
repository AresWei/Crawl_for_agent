"""Local search registration; no optional model or search service required."""
from ..search import answer_local, search_local
from ..search.semantic import search_semantic

CAPABILITIES = {
    "status": "partial_scope",
    "source": "independently authored local SQLite corpus retrieval",
    "implemented": ["local_fts5", "chinese_literal_search", "latest_observed_index",
                    "source_references", "exact_evidence_excerpts", "extractive_evidence_answer",
                    "bounded_cpu_semantic_retrieval", "version_exact_vector_cache"],
    "pending": [],
    "related_jobs": {"web_search": "web.search", "local_model_answering": "answer.model", "search_and_answer": "research.answer"},
    "jobs": {"search.local": {"query": "string", "limit": "1..50, default 10"},
             "answer.local": {"query": "string", "limit": "1..50, default 5"},
             "search.semantic": {"query": "string", "limit": "1..50, default 10",
                "max_records": "1..1000, default 100", "max_chunks": "1..255, default 128",
                "source_job_id": "optional source job scope"}},
}


def register(runner):
    runner.register("search.local", search_local)
    runner.register("answer.local", answer_local)
    runner.register("search.semantic", search_semantic)
