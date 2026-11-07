"""One app-home GPU lease for language, vision and image workers.

The OS lock is authoritative; queue files are observable waiting tickets. All
worker teardown completes before releasing it. The text server restores lazily
on the next language request, avoiding reloads between vision and image jobs.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
import uuid

import httpx
import psutil

from .exclusive import lease as exclusive_lease
from .paths import AppPaths
from .model_profiles import state_profile


class GPUOwnershipError(RuntimeError):
    """A recorded PID or port belongs to something we must not stop."""


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _identity(process: psutil.Process) -> dict:
    return {'pid': process.pid, 'created': process.create_time(),
            'executable': process.exe(), 'argv': process.cmdline()}


def _verified(identity: dict) -> psutil.Process | None:
    try:
        process = psutil.Process(identity['pid'])
        actual = _identity(process)
    except psutil.NoSuchProcess:
        return None
    if actual != identity:
        raise GPUOwnershipError('Recorded GPU process identity changed; nothing was stopped')
    return process


def _stop(identity: dict) -> None:
    process = _verified(identity)
    if process is None:
        return
    process.terminate()  # psutil verifies creation time again before signalling.
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


async def _settle(awaitable):
    """Don't release a GPU lock while cancellation leaves cleanup running."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ManagedLanguageServer:
    def __init__(self, root: Path):
        self.paths = AppPaths.discover(root)
        self.runtime = self.paths.root / 'runtime/model-server'
        self.state_path = self.runtime / 'state.json'
        self.parked_path = self.paths.root / 'state/gpu/parked-language.json'

    def _owned(self) -> tuple[dict, dict] | None:
        if not self.state_path.exists():
            return None
        state = _read(self.state_path)
        try:
            process = psutil.Process(int(state['pid']))
            identity = _identity(process)
        except psutil.NoSuchProcess:
            return None
        executable = Path(state['executable']).resolve()
        model = Path(state['model']).resolve()
        if (not executable.is_relative_to(self.paths.root / 'runtime')
                or not model.is_relative_to(self.paths.root / 'models')
                or Path(identity['executable']).resolve() != executable
                or executable.name not in {'llama-server.exe', 'llama-server'}):
            raise GPUOwnershipError('Local language worker executable is not owned')
        args = identity['argv']
        expected = {'--model': str(model), '--host': '127.0.0.1', '--port': '8947',
                    '--api-key-file': str(self.runtime / 'token'), '--alias': state_profile(self.paths.root,state)['alias']}
        for flag, value in expected.items():
            if flag not in args or args.index(flag) + 1 >= len(args):
                raise GPUOwnershipError('Local language worker arguments are not owned')
            actual = args[args.index(flag) + 1]
            if flag in {'--model', '--api-key-file'}:
                matches = Path(actual).resolve() == Path(value).resolve()
            else:
                matches = actual == value
            if not matches:
                raise GPUOwnershipError('Local language worker arguments changed')
        if '--api-key' in args:
            raise GPUOwnershipError('Inline credentials are not accepted in managed worker arguments')
        self._check_port(identity['pid'])
        return state, identity

    @staticmethod
    def _check_port(allowed_pid: int | None = None) -> None:
        for connection in psutil.net_connections(kind='tcp'):
            if connection.status == psutil.CONN_LISTEN and connection.laddr.port == 8947:
                if connection.pid != allowed_pid or connection.laddr.ip != '127.0.0.1':
                    raise GPUOwnershipError('Port 8947 belongs to an unmanaged process')

    def park(self) -> None:
        owned = self._owned()
        if owned is None:
            self._check_port()
            return
        state, identity = owned
        _write(self.parked_path, {'state': state, 'identity': identity})
        _stop(identity)

    async def restore(self, *, wait_seconds: float = 240) -> None:
        if not self.parked_path.exists():
            return
        saved = _read(self.parked_path)
        owned = self._owned()
        if owned is None:
            self._check_port()
            identity = saved['identity']
            # Validate the saved argv against the original state before execution.
            state = saved['state']
            if Path(identity['executable']).resolve() != Path(state['executable']).resolve():
                raise GPUOwnershipError('Parked executable differs from managed state')
            if not Path(identity['executable']).resolve().is_relative_to(self.paths.root / 'runtime'):
                raise GPUOwnershipError('Parked executable is outside application runtime')
            with (self.runtime / 'stdout.log').open('ab') as stdout, (self.runtime / 'stderr.log').open('ab') as stderr:
                process = subprocess.Popen(identity['argv'], cwd=self.runtime,
                    env=self.paths.child_environment(), stdout=stdout, stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            state = {**state, 'pid': process.pid, 'started_at': datetime.now(timezone.utc).isoformat()}
            _write(self.state_path, state)
        token = (self.runtime / 'token').read_text(encoding='utf-8-sig').strip()
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if self._owned() is None:
                    raise RuntimeError('Language worker exited during restoration; inspect its stderr.log')
                try:
                    response = await client.get('http://127.0.0.1:8947/health',
                        headers={'Authorization': 'Bearer ' + token})
                    if response.status_code == 200:
                        self.parked_path.unlink(missing_ok=True)
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.25)
        raise RuntimeError('Language worker restoration timed out; parked state retained for retry')


class GPUCoordinator:
    def __init__(self, root: Path | str | None = None):
        self.paths = AppPaths.discover(root)
        self.folder = self.paths.root / 'state/gpu'
        self.language = ManagedLanguageServer(self.paths.root)
        self._active = False

    def track_process(self, process) -> None:
        """Register immediately after spawn, before waiting for model loading."""
        if not self._active:
            raise RuntimeError('GPU worker registration requires an active lease')
        identity = _identity(psutil.Process(process.pid))
        _write(self.folder / ('worker-' + str(process.pid) + '.json'), identity)

    def untrack_process(self, process) -> None:
        """Remove only after terminate/wait; a live worker keeps its recovery record."""
        path = self.folder / ('worker-' + str(process.pid) + '.json')
        if path.exists():
            if _verified(_read(path)) is not None:
                raise RuntimeError('GPU worker must exit before it is unregistered')
            path.unlink(missing_ok=True)

    def _cleanup_workers(self) -> None:
        for path in self.folder.glob('worker-*.json'):
            _stop(_read(path))
            path.unlink(missing_ok=True)

    @asynccontextmanager
    async def lease(self, kind: str, *, check_control=None, wait_timeout: float | None = None,
                    restore_timeout: float | None = None, restore_language: bool = True):
        if kind not in {'language', 'vision', 'image', 'asr', 'status'}:
            raise ValueError('Unsupported GPU workload')
        if self._active:
            raise RuntimeError('GPU leases cannot be nested on one coordinator')
        self.folder.mkdir(parents=True, exist_ok=True)
        ticket = self.folder / ('queue-' + uuid.uuid4().hex + '.json')
        _write(ticket, {'pid': os.getpid(), 'created': psutil.Process().create_time(),
                        'kind': kind, 'queued_at': time.time()})

        async def control():
            if check_control is not None:
                await check_control()

        try:
            async with AsyncExitStack() as resources:
                context = SimpleNamespace(paths=self.paths, resources=resources, check_control=control)
                await asyncio.wait_for(exclusive_lease(context, 'gpu', 'device-0'), timeout=wait_timeout)
                if kind == 'status':
                    yield self
                    return
                self._active = True
                ticket.unlink(missing_ok=True)
                # Recover workers left by a crashed owner before waking language.
                try:
                    await _settle(asyncio.to_thread(self._cleanup_workers))
                    await control()
                    if kind == 'language' and restore_language:
                        restoring = (self.language.restore() if restore_timeout is None else
                                     self.language.restore(wait_seconds=restore_timeout))
                        await _settle(restoring)
                    elif kind != 'language':
                        await _settle(asyncio.to_thread(self.language.park))
                    await control()
                    yield self
                finally:
                    async def cleanup():
                        await asyncio.to_thread(self._cleanup_workers)
                    try:
                        await _settle(cleanup())
                    finally:
                        self._active = False
        finally:
            ticket.unlink(missing_ok=True)


async def hardware_snapshot() -> dict:
    """Host-wide measurements, not per-worker allocations or peak guarantees."""
    memory = psutil.virtual_memory()
    result = {'timestamp': time.time(), 'ram_total_bytes': memory.total,
              'ram_available_bytes': memory.available}
    def query():
        return subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.used,memory.free',
            '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        completed = await asyncio.to_thread(query)
        if completed.returncode == 0:
            result['nvidia_smi'] = completed.stdout.strip()
        else:
            result['gpu_measurement'] = 'unavailable'
    except (OSError, subprocess.TimeoutExpired):
        result['gpu_measurement'] = 'unavailable'
    return result
