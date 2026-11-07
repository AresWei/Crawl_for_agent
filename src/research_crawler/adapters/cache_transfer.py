"""Explicit cache transfer; no automatic account/cache discovery."""
from ..web.legacy_cache import import_cache,export_cache
CAPABILITIES={'status':'bounded_source_compatibility','implemented':['original_sqlite_inline','original_content_files','explicit_gzip_extension','atomic_import_cursor','identity_scoped_archived_evidence','original_format_export'],
              'limits':['no_automatic_page_cache_promotion','upstream_xxh64_not_reverified','cache_time_is_not_capture_time','export_limited_to_1000_selected_observations']}
def register(runner):
    runner.register('cache.import',import_cache)
    runner.register('cache.export',export_cache)
