from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from collections import deque
from contextlib import asynccontextmanager

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .protocol import CAPABILITIES, MAX_BYTES, VERSION, SigningError, validate


async def compute(paths, payload):
    """A retained subprocess handle and creation-time identities own this cleanup."""
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'research_crawler.signing.worker',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=paths.child_environment(), cwd=paths.root,
        **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
    owner = psutil.Process(process.pid)
    created = owner.create_time()
    try:
        output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload, ensure_ascii=False).encode()), 15)
        if process.returncode or len(output) > MAX_BYTES:
            raise SigningError('calculation_failed')
        result = json.loads(output)
        if not isinstance(result, dict):
            raise SigningError('calculation_failed')
        return result
    finally:
        descendants = []
        if process.returncode is None:
            try:
                if owner.create_time() == created:
                    descendants = [(p, p.create_time()) for p in owner.children(recursive=True)]
            except psutil.Error:
                pass
            process.kill()
            await process.wait()
        for child, identity in reversed(descendants):
            try:
                if child.is_running() and child.create_time() == identity:
                    child.terminate()
                    try:
                        await asyncio.to_thread(child.wait, timeout=2)
                    except psutil.TimeoutExpired:
                        if child.is_running() and child.create_time() == identity:
                            child.kill()
                            await asyncio.to_thread(child.wait, timeout=2)
            except psutil.Error:
                pass


def create_app(paths, token, *, executor=compute):
    if len(token) < 32:
        raise ValueError('A random service token of at least 32 characters is required')
    lock, recent, active = asyncio.Lock(), deque(), set()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in list(active):
            task.cancel()
        await asyncio.gather(*list(active), return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def authenticated(request):
        # Also reject cross-origin browser calls; the token is a local bearer credential.
        return (request.client is not None and request.client.host in ('127.0.0.1', '::1')
                and not request.headers.get('origin')
                and secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token))

    @app.get('/v1/health')
    async def health(request: Request):
        if not authenticated(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return {'version': VERSION, 'capabilities': CAPABILITIES, 'network_used': False,
                'active': len(active), 'max_request_bytes': MAX_BYTES, 'timeout_seconds': 15,
                'max_concurrency': 1, 'rate_limit_per_minute': 120}

    @app.post('/v1/sign')
    async def sign(request: Request):
        if not authenticated(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        if lock.locked():
            return JSONResponse({'error': 'busy'}, status_code=429)
        now = time.monotonic()
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= 120:
            return JSONResponse({'error': 'rate_limited'}, status_code=429)
        recent.append(now)
        try:
            async with lock:
                raw = bytearray()
                async with asyncio.timeout(5):
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise SigningError('request_too_large')
                payload = validate(json.loads(raw))
                task = asyncio.create_task(executor(paths, payload))
                active.add(task)
                try:
                    result = await task
                finally:
                    active.discard(task)
                return {'version': VERSION, 'platform': payload['platform'], 'result': result}
        except SigningError as error:
            code = str(error)
            return JSONResponse({'error': code}, status_code=422 if code == 'requires_browser' else 400)
        except (ValueError, TypeError, KeyError):
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        except TimeoutError:
            return JSONResponse({'error': 'calculation_timeout'}, status_code=504)
        except OSError:
            return JSONResponse({'error': 'calculation_unavailable'}, status_code=503)

    return app
