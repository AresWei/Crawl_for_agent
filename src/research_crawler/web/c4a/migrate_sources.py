"""Reproduce the frozen C4A source port; no upstream runtime or provider client."""
from pathlib import Path
import hashlib
import json

COMMIT = '838869bdda93f25e6d50b3f50ae9577d9b19a586'


def migrate(root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[4]
    source = root / 'upstream/crawl4ai' / ('crawl4ai-' + COMMIT)
    destination = root / 'src/research_crawler/web/c4a'
    entries = []
    for name in ('__init__.py', 'c4ai_script.py', 'c4a_result.py', 'c4a_compile.py'):
        original = source / 'crawl4ai/script' / name
        body = original.read_text(encoding='utf-8')
        changes = ['Attribution header; normalized UTF-8 LF encoding']
        if name == 'c4a_compile.py':
            body = body.replace('from ..prompts import GENERATE_JS_SCRIPT_PROMPT, GENERATE_SCRIPT_PROMPT', '')
            body = body.replace('from ..async_configs import LLMConfig', '')
            body = body.replace('from ..utils import perform_completion_with_backoff', '')
            body = body.replace('from .c4ai_script import Compiler', 'from .bounded_compiler import Compiler')
            start = body.index('    @staticmethod\n    def generate_script(')
            end = body.index('\n\n# Convenience functions', start)
            body = body[:start] + '''    @staticmethod
    async def generate_script(html, query=None, mode="c4a", *, ctx, **kwargs):
        """Local-only async generation, using the product-owned model lifecycle.

        Returns a draft only. Generation never executes browser actions.
        """
        from .generation import generate_script
        return await generate_script(html, query, mode=mode, ctx=ctx, **kwargs)
''' + body[end:]
            changes += ['Use bounded subclass of source compiler', 'Replace cloud completion helper with explicit async shared local-model draft generation']
        header = f'# Source-derived from Crawl4AI {COMMIT}, crawl4ai/script/{name}.\n# Copyright original Crawl4AI authors; see LICENSE.crawl4ai and ../NOTICE.\n# Modifications: {"; ".join(changes)}.\n'
        output = destination / name
        output.write_bytes((header + body).encode('utf-8'))
        entries.append({'source': str(original.relative_to(root)).replace('\\','/'), 'destination': str(output.relative_to(root)).replace('\\','/'), 'source_sha256': hashlib.sha256(original.read_bytes()).hexdigest(), 'destination_sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'changes': changes})
    license_source = source / 'LICENSE'
    license_output = destination / 'LICENSE.crawl4ai'
    license_output.write_bytes(license_source.read_bytes())
    entries.append({'source': str(license_source.relative_to(root)).replace('\\','/'), 'destination': str(license_output.relative_to(root)).replace('\\','/'), 'source_sha256': hashlib.sha256(license_source.read_bytes()).hexdigest(), 'destination_sha256': hashlib.sha256(license_output.read_bytes()).hexdigest(), 'changes': []})
    (destination / 'source-manifest.json').write_text(json.dumps({'upstream_commit': COMMIT, 'files': entries}, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    migrate()
