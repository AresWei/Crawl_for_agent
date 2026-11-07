"""Register source-derived URL seeding and bounded domain mapping."""
from ..discovery.runtime import discover

CAPABILITIES = {
    "status": "partial_upstream_scope", "source": "Crawl4AI 838869bdda93f25e6d50b3f50ae9577d9b19a586 + shared core driver",
    "implemented": ["sitemap_index", "gzip_sitemap", "robots_sitemap", "common_crawl_index", "explicit_host_scope",
                    "rss_atom", "homepage_links", "explicit_path_probes", "soft404_fingerprint", "live_head_check",
                    "head_metadata", "url_relevance", "bm25_if_installed", "atomic_frontier_resume",
                    "common_crawl_page_cursors", "wayback_resume_key", "certificate_host_candidates"],
    "pending": ["guessed_subdomains", "upstream_disk_cache_compatibility",
                "common_crawl_multi_index", "original_typed_configuration_api", "external_discovery_live_availability"],
    "jobs": {"web.seed": {"url": "website or sitemap URL", "sources": "robots,sitemap,cc", "max_urls": "1..1000"},
             "web.map": {"url": "selected website", "sources": "robots,sitemap,homepage,feed,cc,probe,wayback,crt",
                         "probe_paths": "required for probe; 1..20 explicit paths"}},
}


def register(runner):
    runner.register("web.seed", discover)
    async def mapping(context):
        return await discover(context, mapping=True)
    runner.register("web.map", mapping)
