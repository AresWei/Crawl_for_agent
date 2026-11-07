"""Unified web job adapter; no upstream subprocess or installed Crawl4AI dependency."""
from ..web.runtime import collect_web

CAPABILITIES = {
    "status": "partial_scope",
    "source": "Crawl4AI 0.9.5 source-integrated algorithms",
    "implemented": ["http", "dynamic_dom", "markdown", "citations", "pruning", "bm25", "css_schema", "lxml_schema", "xpath_schema",
                    "regex", "deterministic_chunking", "bfs", "dfs", "best_first", "url_filters", "url_scorers", "same_site_discovery",
                    "raw_html", "local_html", "native_pdf", "screenshot", "pdf_export", "mhtml", "cache_modes", "cache_provenance",
                    "conditional_cache_validation", "lxml_scraper", "tables", "metadata", "media_candidates", "bounded_dispatch",
                    "browser_context_options", "local_cdp_attach", "exact_cdp_target", "iframe_processing", "open_shadow_dom",
                    "overlay_removal", "bounded_virtual_scroll", "local_llm_schema_extraction", "bounded_link_previews",
                    "local_cosine_clustering", "local_topic_labels", "nlp_sentence_and_topic_chunking", "chunk_source_ranges",
                    "c4a_language", "browser_downloads", "bounded_browser_diagnostics", "browser_tls_summary",
                    "persistent_browser_profiles", "session_page_ttl", "tls_certificate_exports", "local_llm_content_filter",
                    "memory_adaptive_dispatch", "bounded_get_retry", "persistent_host_backoff", "dispatch_url_matching",
                    "firefox_webkit", "optional_patchright", "source_browser_hook_contract", "bounded_javascript_variants",
                    "local_schema_inference_feedback_cache", "local_llm_tables_with_position_validation",
                    "completion_order_page_publication", "statistical_adaptive_exploration", "durable_adaptive_knowledge_base",
                    "continuous_stream_refill", "bfs_host_round_robin", "local_adaptive_embeddings", "local_query_expansion",
                    "typed_config_clone_serialization", "dedicated_crawler_registry",
                    "per_url_run_configuration", "shared_config_resource_scope", "stitched_screenshot_receipts",
                    "http_form_submission", "scoped_http_tls_verification", "http_charset_detection",
                    "opt_in_block_detection", "blocked_source_answer_exclusion",
                    "redirect_method_and_credential_scope", "css_js_waits", "session_steps", "resume", "cancel"],
    "pending": ["full_upstream_config", "cross_job_live_pages", "remote_cdp", "empty_cdp_context_selection", "full_cross_engine_options", "upstream_stealth_adapter",
                "full_hook_config_object_compatibility", "closed_shadow_dom", "original_stream_result_container_api", "adaptive_semantic_quality_acceptance", "full_url_seeder_parity", "full_domain_mapper_parity",
                "schema_inference_general_semantic_accuracy", "llm_multilevel_nested_tables", "crawler_live_layout_coverage", "full_ssl_certificate_chain", "cross_engine_stitched_acceptance"],
    "acceptance": "docs/audits/web-extended-migration.md",
}


def register(runner):
    runner.register("web.collect", collect_web)
