# Zhihu wrapper adapted from the integrated MediaCrawler 54a3d924 client.
# Copyright (c) 2025 relakkes@gmail.com; NON-COMMERCIAL LEARNING LICENSE 1.1.
# Algorithms remain in their attributed platform assets; no private Pro code.
"""Reuse integrated public-source algorithms; retains their respective licenses."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from .protocol import SigningError, validate


async def zhihu_headers(paths, path, dc0):
    node = shutil.which('node')
    if not node:
        raise SigningError('node_dependency_missing')
    asset = Path(__file__).parents[1] / 'platforms/zhihu/zhihu.js'
    script = "const fs=require('fs'),vm=require('vm');vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>{const v=JSON.parse(s);process.stdout.write(JSON.stringify(get_sign(v.path,'d_c0='+v.dc0)));});"
    process = await asyncio.create_subprocess_exec(node, '-e', script, str(asset),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=paths.child_environment(), **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
    try:
        output, _ = await asyncio.wait_for(process.communicate(json.dumps({'path': path, 'dc0': dc0}).encode()), 10)
        if process.returncode:
            raise SigningError('signer_failed')
        signed = json.loads(output)
        return {'x-zst-81': signed['x-zst-81'], 'x-zse-96': signed['x-zse-96'], 'x-zse-93': '101_3_3.0'}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def execute(request, context):
    validate(request)
    platform, data = request['platform'], request['payload']
    if platform == 'zhihu':
        return await zhihu_headers(context.paths, data['path'], data['dc0'])
    if platform == 'dy':
        from ..platforms.douyin import DouyinSigner
        return {'a_bogus': await DouyinSigner(context).sign(data['path'], data['query'], data['ua'])}
    from xhshow import Xhshow
    signer = Xhshow()
    method = signer.sign_headers_post if data['method'] == 'POST' else signer.sign_headers_get
    return method(uri=data['path'], cookies=data['cookies'],
                  **({'payload': data['values']} if data['method'] == 'POST' else {'params': data['values']}))
