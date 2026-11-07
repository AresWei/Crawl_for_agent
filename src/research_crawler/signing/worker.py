"""One bounded calculation process. Stdin/stdout are private, never receipt logs."""
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from types import SimpleNamespace
from ..core.paths import AppPaths
from .local import execute
from .protocol import MAX_BYTES


async def main():
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('request_too_large')
    async with AsyncExitStack() as resources:
        context = SimpleNamespace(paths=AppPaths.discover(), params={'signing_backend': 'local'}, resources=resources)
        result = await execute(json.loads(raw), context)
    sys.stdout.write(json.dumps(result))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        sys.stderr.write('signing_failed\n')
        sys.exit(1)
