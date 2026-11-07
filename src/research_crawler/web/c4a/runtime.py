"""Execute source-compiled C4A in the existing task page and cancellation scope."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from pathlib import Path

from ...core.jobs import BudgetExceeded, JobControl
from ...core.sessions import controlled
from .c4a_compile import C4ACompiler


def _safe_errors(errors):
    # Source lines, selectors, EVAL values and browser error text may hold secrets.
    return [{'code': error.code, 'type': error.type.value,
             'location': {'line': error.line, 'column': error.column}}
            for error in errors]


async def execute_c4a(page, params, ctx):
    script = params.get('c4a_script')
    if script is None:
        return None
    if params.get('cdp_url'):
        raise ValueError('C4A execution requires an owned task page, not a borrowed CDP tab')
    if isinstance(script, list) and all(isinstance(line, str) for line in script):
        script = '\n'.join(script)
    if not isinstance(script, str) or len(script.encode('utf-8')) > 128_000:
        raise ValueError('c4a_script must be text or text lines, at most 128000 bytes')
    timeout = params.get('c4a_timeout_seconds', 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 300:
        raise ValueError('c4a_timeout_seconds must be finite, in 0.1..300')
    root = params.get('c4a_include_root')
    if root is not None:
        root = Path(root).resolve()
        if not root.is_relative_to(ctx.paths.root.resolve()) or not root.is_dir():
            raise ValueError('c4a_include_root must be an existing directory under the project root')
    compiled = C4ACompiler.compile(script, root=root)
    result = {'success': compiled.success, 'script_sha256': hashlib.sha256(script.encode()).hexdigest(),
              'statement_count': len(compiled.js_code or []), 'completed_statements': 0,
              'errors': _safe_errors(compiled.errors), 'engine': 'source-c4a-compiler',
              'event_semantics': 'synthetic_dom_events'}
    if not compiled.success:
        result['status'] = 'compilation_error'
        return result
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            for index, javascript in enumerate(compiled.js_code):
                await ctx.check_control()
                # Source navigation expressions need a browser-owned navigation wait.
                # Top-level GO uses goto directly to avoid destroying an evaluate context.
                if javascript.startswith('window.location.href = '):
                    url = json.loads(javascript.removeprefix('window.location.href = ').removesuffix(';'))
                    response = await controlled(ctx, page.goto(url, wait_until='domcontentloaded'))
                    result['navigation_status'] = response.status if response else None
                    if response and response.status >= 400:
                        raise ValueError('C4A navigation HTTP error')
                elif javascript in ('window.location.reload();', 'window.history.back();', 'window.history.forward();'):
                    method = {'window.location.reload();': page.reload, 'window.history.back();': page.go_back,
                              'window.history.forward();': page.go_forward}[javascript]
                    response = await controlled(ctx, method(wait_until='domcontentloaded'))
                    result['navigation_status'] = response.status if response else None
                    if response and response.status >= 400:
                        raise ValueError('C4A navigation HTTP error')
                else:
                    await controlled(ctx, page.evaluate('async () => {\n' + javascript + '\n}'))
                result['completed_statements'] = index + 1
        result['status'] = 'complete'
    except (JobControl, BudgetExceeded, asyncio.CancelledError):
        # Cancelled evaluation does not stop page-side timers. Dispose the affected
        # page before same-job session reuse can execute their delayed actions.
        await page.close()
        raise
    except TimeoutError:
        result.update(success=False, status='timeout', errors=[{'code': 'C4A_TIMEOUT'}])
        await page.close()
    except Exception as error:
        result.update(success=False, status='execution_error', errors=[{'code': 'C4A_EXECUTION', 'type': type(error).__name__}])
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result
