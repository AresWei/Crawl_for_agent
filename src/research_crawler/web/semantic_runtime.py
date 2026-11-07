"""Bounded isolated CPU worker shared by collection and local semantic search."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time


class SemanticRuntime:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _launch(self):
        environment_name = 'semantic-model-venv'
        python = self.root / 'runtime' / environment_name / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        from ..core.paths import AppPaths
        environment = AppPaths(self.root).child_environment()
        environment.update({'PYTHONPATH':str(self.root/'src'),'PYTHONUTF8':'1',
            'NLTK_DATA':str(self.root/'cache/nltk'),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
            'CUDA_VISIBLE_DEVICES':'','OMP_NUM_THREADS':'2','TOKENIZERS_PARALLELISM':'false'})
        return [str(python), str(self.root/'scripts/semantic-worker.py')], environment

    def run(self, request: dict, *, timeout: float = 120) -> dict:
        command, environment = self._launch()
        started = time.perf_counter()
        process = subprocess.Popen(command,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',
            cwd=self.root, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            stdout, stderr = process.communicate(json.dumps(request,ensure_ascii=False),timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_tree(process.pid)
            process.communicate()
            raise
        if process.returncode:
            raise RuntimeError('Semantic worker failed; inspect the local model installation and bounded inputs')
        result = json.loads(stdout)
        result.setdefault('metrics', {})['subprocess_wall_ms'] = round((time.perf_counter()-started)*1000, 3)
        return result

    async def arun(self, request: dict, *, timeout: float = 120) -> dict:
        command, environment = self._launch()
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=self.root,env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        communication = asyncio.create_task(process.communicate(json.dumps(request,ensure_ascii=False).encode('utf-8')))
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            async def cleanup():
                await asyncio.to_thread(_stop_tree, process.pid)
                await communication
                await process.wait()
            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
            raise
        if process.returncode:
            raise RuntimeError('Semantic worker failed; inspect the local model installation and bounded inputs')
        result = json.loads(stdout)
        result.setdefault('metrics',{})['subprocess_wall_ms'] = round((time.perf_counter()-started)*1000,3)
        return result


def _stop_tree(pid):
    """Windows venv launchers have a child Python; settle both before lease release."""
    import psutil
    try:
        parent = psutil.Process(pid)
        processes = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes,timeout=2)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive,timeout=2)
